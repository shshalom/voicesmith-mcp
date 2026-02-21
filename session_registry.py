"""
Session registry for multi-session coordination.

Tracks active MCP server sessions in a shared JSON file.
Uses flock for safe concurrent access.
"""

import fcntl
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from shared import (
    SESSIONS_FILE_NAME,
    DEFAULT_HTTP_PORT,
    VOICE_NAME_MAP,
    ALL_VOICE_IDS,
    get_logger,
)

logger = get_logger("session-registry")


def _sessions_path() -> Path:
    """Return the path to the sessions file."""
    return Path.home() / ".local" / "share" / "agent-voice-mcp" / SESSIONS_FILE_NAME


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_sessions(path: Path) -> list[dict]:
    """Read sessions from file (caller must hold flock)."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("sessions", [])
    except (json.JSONDecodeError, OSError):
        return []


def _write_sessions(path: Path, sessions: list[dict]) -> None:
    """Write sessions to file (caller must hold flock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"sessions": sessions}, f, indent=2)


def _clean_stale(sessions: list[dict]) -> list[dict]:
    """Remove sessions whose PID is no longer alive."""
    alive = []
    for s in sessions:
        if _pid_alive(s.get("pid", 0)):
            alive.append(s)
        else:
            logger.info(f"Removed stale session: {s.get('name')} (pid {s.get('pid')})")
    return alive


def _find_available_name(taken_names: set[str], preferred: str) -> tuple[str, str]:
    """Find an available voice name.

    Returns (name, voice_id). Tries the preferred name first,
    then picks from Kokoro voice names not already taken.
    """
    # Try preferred name
    preferred_lower = preferred.lower()
    if preferred not in taken_names and preferred_lower in VOICE_NAME_MAP:
        return preferred, VOICE_NAME_MAP[preferred_lower]

    # Pick the next available Kokoro voice name
    for name_lower, voice_id in sorted(VOICE_NAME_MAP.items()):
        name = name_lower.capitalize()
        if name not in taken_names:
            return name, voice_id

    # All names taken — shouldn't happen with 54 voices, but fallback
    return preferred, VOICE_NAME_MAP.get(preferred_lower, "am_eric")


def _find_available_port(sessions: list[dict], base_port: int) -> int:
    """Find the lowest available port starting from base_port."""
    used_ports = {s.get("port") for s in sessions}
    port = base_port
    while port in used_ports:
        port += 1
    return port


def register_session(
    preferred_name: str,
    preferred_voice: str,
    base_port: int = DEFAULT_HTTP_PORT,
) -> dict:
    """Register this server as an active session.

    Returns the session dict with assigned name, voice, and port.
    Uses flock on sessions.json for safe concurrent access.
    """
    path = _sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Touch the file so flock has something to lock
    if not path.exists():
        _write_sessions(path, [])

    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)

        sessions = _read_sessions(path)
        sessions = _clean_stale(sessions)

        taken_names = {s["name"] for s in sessions}

        if preferred_name in taken_names:
            name, voice = _find_available_name(taken_names, preferred_name)
            logger.warning(
                f"'{preferred_name}' already active. Assigned '{name}' ({voice}) instead."
            )
        else:
            name = preferred_name
            voice = preferred_voice

        port = _find_available_port(sessions, base_port)

        session = {
            "name": name,
            "voice": voice,
            "port": port,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        sessions.append(session)
        _write_sessions(path, sessions)

        # flock released when f closes

    logger.info(f"Session registered: {name} ({voice}) on port {port}")
    return session


def unregister_session() -> None:
    """Remove this server's session from the registry."""
    path = _sessions_path()
    if not path.exists():
        return

    pid = os.getpid()

    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            sessions = _read_sessions(path)
            sessions = [s for s in sessions if s.get("pid") != pid]
            _write_sessions(path, sessions)
    except OSError as e:
        logger.warning(f"Failed to unregister session: {e}")

    logger.info("Session unregistered")


def get_active_sessions() -> list[dict]:
    """Return list of active sessions (stale PIDs filtered out)."""
    path = _sessions_path()
    if not path.exists():
        return []

    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            sessions = _read_sessions(path)
            alive = _clean_stale(sessions)
            if len(alive) != len(sessions):
                _write_sessions(path, alive)
            return alive
    except OSError:
        return []
