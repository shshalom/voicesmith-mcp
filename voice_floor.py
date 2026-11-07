"""
Voice floor: cross-process arbitration of the shared audio channel.

Every MCP server session is a separate process, but all of them share one
speaker and one microphone. The voice floor is a single kernel-managed
flock that must be held for ANY use of that channel — speaking or
listening. Sessions queue politely instead of talking over each other or
recording each other's TTS as user speech.

Properties:
- flock-based: no polling daemons, no PID liveness checks. The kernel
  releases the lock automatically when a holder crashes — no stale locks.
- Reentrant per asyncio task / thread: `speak_then_listen` acquires the
  floor once and calls `speak` + `listen` internally without deadlocking.
- Waiters poll the lock non-blockingly every 50 ms (async sleep, so the
  event loop stays live) and time out with a FloorBusyError that names the
  current holder via a sidecar info file.
"""

import asyncio
import errno
import fcntl
import json
import os
import threading
import time
from typing import Optional

from shared import FLOOR_LOCK_PATH, FLOOR_INFO_PATH, get_logger

logger = get_logger("voice-floor")

POLL_INTERVAL = 0.05  # seconds between lock probes while waiting


class FloorBusyError(Exception):
    """Raised when the voice floor cannot be acquired within the timeout."""

    def __init__(self, holder: Optional[dict]):
        self.holder = holder
        name = (holder or {}).get("name") or "another session"
        purpose = (holder or {}).get("purpose") or "audio"
        super().__init__(f"Voice floor is held by {name} ({purpose})")


def _owner_key():
    """Identify the current acquirer: asyncio task, or thread as fallback.

    Reentrancy is keyed on this, so nested acquires from the same tool
    call (e.g. speak_then_listen → speak → listen) share one hold.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return task or threading.current_thread()


class VoiceFloor:
    """Cross-process mutex over the speaker + microphone.

    One instance per process, all contending on the same lock file. The
    per-instance state (_fd/_owner/_depth) only tracks this process's own
    reentrancy; exclusion across processes is enforced by flock itself.
    """

    def __init__(self, lock_path: str = FLOOR_LOCK_PATH, info_path: str = FLOOR_INFO_PATH):
        self._lock_path = lock_path
        self._info_path = info_path
        self._state_lock = threading.Lock()
        self._fd: Optional[int] = None
        self._owner = None
        self._depth = 0

    # ─── Acquisition ────────────────────────────────────────────────────────

    async def acquire(
        self,
        purpose: str,
        session_name: str = "",
        wait_timeout: float = 20.0,
    ) -> None:
        """Acquire the floor from async code. Reentrant per task.

        Raises FloorBusyError if another session still holds the floor
        after wait_timeout seconds.
        """
        key = _owner_key()
        with self._state_lock:
            if self._fd is not None and self._owner is key:
                self._depth += 1
                return

        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        holder = None
        try:
            deadline = time.monotonic() + max(0.0, wait_timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    holder = self.holder_info()
                    if time.monotonic() >= deadline:
                        raise FloorBusyError(holder)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                # Acquired. No awaits between here and bookkeeping, so a
                # cancelled task can never leave a held-but-forgotten lock.
                with self._state_lock:
                    self._fd = fd
                    self._owner = key
                    self._depth = 1
                fd = None
                self._write_holder(purpose, session_name)
                return
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def acquire_sync(
        self,
        purpose: str,
        session_name: str = "",
        wait_timeout: float = 20.0,
    ) -> None:
        """Blocking acquire for non-asyncio threads (wake listener, intro)."""
        key = _owner_key()
        with self._state_lock:
            if self._fd is not None and self._owner is key:
                self._depth += 1
                return

        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        holder = None
        try:
            deadline = time.monotonic() + max(0.0, wait_timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    holder = self.holder_info()
                    if time.monotonic() >= deadline:
                        raise FloorBusyError(holder)
                    time.sleep(POLL_INTERVAL)
                    continue
                with self._state_lock:
                    self._fd = fd
                    self._owner = key
                    self._depth = 1
                fd = None
                self._write_holder(purpose, session_name)
                return
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def release(self) -> None:
        """Release one acquire. Fully releases at depth 0."""
        key = _owner_key()
        with self._state_lock:
            if self._fd is None or self._owner is not key:
                logger.warning("Voice floor release() without matching acquire()")
                return
            self._depth -= 1
            if self._depth > 0:
                return
            fd = self._fd
            self._fd = None
            self._owner = None
            self._depth = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as e:
            logger.warning(f"Failed to unlock voice floor: {e}")
        try:
            os.close(fd)
        except OSError:
            pass
        self._clear_holder()

    # ─── Introspection ──────────────────────────────────────────────────────

    def holder_info(self) -> Optional[dict]:
        """Return the current holder's info dict, or None if the floor is free.

        Verifies the lock itself with a throwaway probe fd, so a sidecar
        left behind by a crashed holder is detected as stale and removed.
        """
        probe = None
        try:
            probe = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    # Held — report sidecar info (best effort).
                    return self._read_holder()
                raise
            fcntl.flock(probe, fcntl.LOCK_UN)
        except OSError as e:
            logger.debug(f"Voice floor probe failed: {e}")
            return None
        finally:
            if probe is not None:
                try:
                    os.close(probe)
                except OSError:
                    pass
        # Lock is free — any sidecar is stale from a crashed holder.
        self._clear_holder()
        return None

    # ─── Sidecar (advisory holder info) ─────────────────────────────────────

    def _write_holder(self, purpose: str, session_name: str) -> None:
        info = {
            "pid": os.getpid(),
            "name": session_name or None,
            "purpose": purpose,
            "acquired_at": round(time.time(), 3),
        }
        try:
            tmp = self._info_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(info, f)
            os.replace(tmp, self._info_path)
        except OSError as e:
            logger.debug(f"Failed to write floor sidecar: {e}")  # advisory only

    def _read_holder(self) -> Optional[dict]:
        try:
            with open(self._info_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _clear_holder(self) -> None:
        try:
            os.unlink(self._info_path)
        except OSError:
            pass


# ─── Process-wide default instance ───────────────────────────────────────────

_default_floor: Optional[VoiceFloor] = None


def get_default_floor() -> VoiceFloor:
    """Return this process's shared VoiceFloor instance.

    server.py and wake_listener.py both use it, so their reentrancy
    state (depth/owner) is shared within the process.
    """
    global _default_floor
    if _default_floor is None:
        _default_floor = VoiceFloor()
    return _default_floor


def current_holder() -> Optional[dict]:
    """Cheap check used by the wake listener: who holds the floor right now?

    Returns the holder dict (any process, including this one), or None.
    Reads the lock + sidecar without instantiating per-process state.
    """
    return get_default_floor().holder_info()