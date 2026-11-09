"""
Thin client engines for the VoiceSmith engine service.

Replace the in-process KokoroEngine / WhisperEngine inside an MCP session
with remote proxies: synthesize/transcribe are forwarded over a Unix
socket to the shared engine service (engine_service.py), so N concurrent
sessions share ONE copy of the ~1.5 GB model stack instead of one each.

The engines expose the same interface as their local counterparts
(synthesize / transcribe / is_loaded), so SpeechQueue, listen, wake, and
export_speech work unchanged. If the service isn't running, the first
request spawns it (flock-guarded against double spawns) and waits for
models to load.
"""

import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from shared import (
    ENGINE_SOCKET_PATH,
    ENGINE_SPAWN_LOCK_PATH,
    SynthesisResult,
    TranscriptionResult,
    TTSEngineError,
    STTEngineError,
    get_logger,
)

logger = get_logger("engine-client")

_HEADER = struct.Struct(">I")
_CONNECT_TIMEOUT = 2.0      # seconds, per connect attempt
_STARTUP_TIMEOUT = 90.0     # seconds, waiting for a freshly spawned service
_REQUEST_TIMEOUT = 180.0    # seconds, per request (models are already warm)


class EngineServiceError(Exception):
    """Raised when the engine service can't be reached or returns an error."""


# ─── Framing ──────────────────────────────────────────────────────────────────

def _frame(req: dict, payload: bytes) -> bytes:
    req = dict(req)
    req["payload_len"] = len(payload)
    header = json.dumps(req).encode()
    return _HEADER.pack(len(header)) + header + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("service closed connection mid-frame")
        buf += chunk
    return buf


def _read_response(sock: socket.socket) -> tuple[dict, bytes]:
    json_len = _HEADER.unpack(_recv_exact(sock, 4))[0]
    if json_len == 0 or json_len > 1024 * 1024:
        raise EngineServiceError(f"bad response header length: {json_len}")
    resp = json.loads(_recv_exact(sock, json_len))
    payload_len = int(resp.get("payload_len", 0))
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return resp, payload


# ─── Connection / spawn ────────────────────────────────────────────────────────

def _try_connect(socket_path: str) -> socket.socket:
    """Attempt a single connect. Raises OSError/ConnectionError on failure."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT)
    try:
        sock.connect(socket_path)
    except OSError:
        sock.close()
        raise
    sock.settimeout(_REQUEST_TIMEOUT)
    return sock


def spawn_service() -> None:
    """Spawn the engine service daemon (flock-guarded against double spawns).

    The daemon itself holds the same lock for its lifetime, so even if two
    sessions spawn concurrently, exactly one survives.
    """
    guard = None
    try:
        guard = open(ENGINE_SPAWN_LOCK_PATH, "w")
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # a spawn is already in flight
    try:
        here = Path(__file__).resolve().parent
        script = here / "engine_service.py"
        if not script.is_file():
            logger.warning(f"engine_service.py not found at {script}")
            return
        # Append to a log in the config dir (fixed path; never user input).
        log_path = Path.home() / ".local" / "share" / "voicesmith-mcp" / "engine.log"
        log_file = open(log_path, "ab")
        try:
            subprocess.Popen(
                [sys.executable, str(script)],
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(here),
                close_fds=True,
            )
            logger.info("Spawned engine service")
        finally:
            log_file.close()
    except Exception as e:
        logger.warning(f"Failed to spawn engine service: {e}")
    finally:
        if guard is not None:
            guard.close()  # releases flock


def connect(spawn: bool = True, socket_path: str = ENGINE_SOCKET_PATH,
            startup_timeout: float = _STARTUP_TIMEOUT) -> socket.socket:
    """Connect to the engine service, spawning it if needed."""
    try:
        return _try_connect(socket_path)
    except OSError:
        if not spawn:
            raise

    spawned_here = False
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if not spawned_here:
            spawn_service()
            spawned_here = True
        time.sleep(0.25)
        try:
            return _try_connect(socket_path)
        except OSError:
            continue
    raise EngineServiceError(
        "Engine service did not become ready in time — "
        f"check ~/.local/share/voicesmith-mcp/engine.log"
    )


def request(op: str, params: dict | None = None, payload: bytes = b"",
            spawn: bool = True, socket_path: str = ENGINE_SOCKET_PATH) -> tuple[dict, bytes]:
    """Send one framed request; return (response dict, payload bytes)."""
    req = {"op": op, **(params or {})}
    sock = connect(spawn=spawn, socket_path=socket_path)
    try:
        sock.sendall(_frame(req, payload))
        resp, resp_payload = _read_response(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not resp.get("ok"):
        raise EngineServiceError(resp.get("error", f"{op} failed"))
    return resp, resp_payload


def service_status(socket_path: str = ENGINE_SOCKET_PATH) -> dict:
    """Best-effort status of the engine service. Never spawns."""
    try:
        resp, _ = request("status", spawn=False, socket_path=socket_path)
        return {"running": True, **resp}
    except (OSError, EngineServiceError):
        return {"running": False}


# ─── Remote engines (same interface as the local ones) ─────────────────────────

class RemoteTTSEngine:
    """TTS engine proxy. Models live in the shared engine service."""

    def __init__(self, socket_path: str = ENGINE_SOCKET_PATH) -> None:
        self._socket_path = socket_path

    def is_loaded(self) -> bool:
        """Always True in remote mode — models are loaded on demand in the
        service; failures surface at synthesize time as TTSEngineError."""
        return True

    def synthesize(self, text: str, voice_id: str, speed: float = 1.0) -> SynthesisResult:
        try:
            resp, payload = request(
                "synthesize",
                {"text": text, "voice": voice_id, "speed": speed},
                socket_path=self._socket_path,
            )
        except EngineServiceError as e:
            raise TTSEngineError(f"engine service: {e}") from e
        samples = np.frombuffer(payload, dtype=np.float32).copy()
        return SynthesisResult(
            samples=samples,
            sample_rate=int(resp["sample_rate"]),
            duration_ms=float(resp.get("duration_ms", 0.0)),
            synthesis_ms=float(resp.get("synthesis_ms", 0.0)),
        )


class RemoteSTTEngine:
    """STT engine proxy. Models live in the shared engine service."""

    def __init__(self, socket_path: str = ENGINE_SOCKET_PATH) -> None:
        self._socket_path = socket_path

    def is_loaded(self) -> bool:
        """Always True in remote mode — see RemoteTTSEngine.is_loaded."""
        return True

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        pcm = np.ascontiguousarray(audio, dtype=np.float32).tobytes()
        try:
            resp, _ = request(
                "transcribe",
                {"sample_rate": sample_rate},
                payload=pcm,
                socket_path=self._socket_path,
            )
        except EngineServiceError as e:
            raise STTEngineError(f"engine service: {e}") from e
        return TranscriptionResult(
            text=resp.get("text", ""),
            confidence=float(resp.get("confidence", 0.0)),
            transcription_ms=float(resp.get("transcription_ms", 0.0)),
            language=resp.get("language", ""),
        )