"""
VoiceSmith Engine Service — shared model host.

Every MCP session is a separate process, and each one used to load its own
copy of the Kokoro ONNX TTS model and the faster-whisper/CTranslate2 STT
stack (~1.5 GB per session). With many agent threads that is tens of GB.

This daemon hosts those models ONCE. Session servers become thin clients
(`engine_client.py`) that send synthesize/transcribe requests over a Unix
socket and get PCM / text back. One daemon + N thin clients.

Lifecycle:
- Spawned on demand by the first client that needs it (with a flock guard
  so concurrent sessions can't double-spawn; losers exit quietly).
- Single-threaded request handling: models are singletons, and callers
  are mostly serialized by the voice floor anyway.
- Exits after a configurable idle period (engine.idle_exit_min, default
  30 min; 0 = stay resident) — the next request respawns it.
- Watches config.json and reloads the STT model if its size changes.
"""

import fcntl
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from shared import (
    ENGINE_SOCKET_PATH,
    ENGINE_SPAWN_LOCK_PATH,
    get_logger,
)
from config import load_config, get_config_path

logger = get_logger("engine-service")

_HEADER = struct.Struct(">I")  # 4-byte big-endian JSON header length
_MAX_JSON_BYTES = 1 * 1024 * 1024      # 1 MB
_MAX_PAYLOAD_BYTES = 256 * 1024 * 1024  # 256 MB
_CONFIG_CHECK_INTERVAL = 15.0  # seconds


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket. Raises ConnectionError on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        buf += chunk
    return buf


class EngineService:
    """Hosts the TTS + STT models and serves requests over a Unix socket."""

    def __init__(
        self,
        socket_path: str = ENGINE_SOCKET_PATH,
        lock_path: str = ENGINE_SPAWN_LOCK_PATH,
        tts_factory: Optional[Callable] = None,
        stt_factory: Optional[Callable] = None,
        idle_exit_s: Optional[float] = None,
        watch_config: bool = True,
    ) -> None:
        self._socket_path = socket_path
        self._lock_path = lock_path
        self._tts_factory = tts_factory
        self._stt_factory = stt_factory
        self._idle_exit_s = idle_exit_s
        self._watch_config = watch_config

        self._tts = None
        self._stt = None
        self._stt_model_size: Optional[str] = None
        self._lock_file = None
        self._server: Optional[socketserver.UnixStreamServer] = None
        self._last_request = time.time()
        self._requests = 0
        self._started = time.time()
        self._config_mtime: float = 0.0
        self._exit = threading.Event()

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Acquire the single-instance lock, load models, bind the socket.

        Returns False if another engine service is already running
        (in which case this instance exits without touching the socket).
        """
        # Single-instance guard: held for the lifetime of this process.
        try:
            self._lock_file = open(self._lock_path, "w")
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.info("Another engine service is already running — exiting")
            return False

        self._load_models()
        self._watch_config_changes()

        # Remove any stale socket left by a crashed instance before binding.
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

        service = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                service._handle_connection(self.request)

        self._server = socketserver.UnixStreamServer(self._socket_path, _Handler)
        self._server.daemon_threads = False
        os.chmod(self._socket_path, 0o600)

        if self._idle_exit_s is not None or self._watch_config:
            threading.Thread(target=self._watchdog, name="engine-watchdog", daemon=True).start()

        logger.info(
            f"Engine service ready on {self._socket_path} "
            f"(TTS: {'OK' if self._tts else 'unavailable'}, "
            f"STT: {'OK' if self._stt else 'unavailable'})"
        )
        return True

    def serve_forever(self) -> None:
        """Serve until SIGTERM/SIGINT or idle exit."""
        def _on_signal(signum, frame):
            # shutdown() blocks until the serve loop notices, so it must
            # run on a different thread — calling it directly from a signal
            # handler deadlocks (the handler runs on the serve loop's thread).
            self._exit.set()
            if self._server is not None:
                threading.Thread(target=self._server.shutdown, daemon=True).start()

        # Signal handlers can only be installed from the main thread
        # (tests run the service loop in a daemon thread).
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _on_signal)
            signal.signal(signal.SIGINT, _on_signal)
        try:
            self._server.serve_forever(poll_interval=0.5)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if self._server is not None:
            try:
                self._server.server_close()
            except OSError:
                pass
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass
        logger.info("Engine service stopped")

    # ─── Model loading ──────────────────────────────────────────────────────

    def _load_models(self) -> None:
        """Load TTS/STT engines, tolerating individual failures."""
        if self._tts_factory is not None:
            try:
                self._tts = self._tts_factory()
            except Exception as e:
                logger.error(f"TTS engine failed to load: {e}")
                self._tts = None
        if self._stt_factory is not None:
            try:
                self._stt = self._stt_factory()
            except Exception as e:
                logger.error(f"STT engine failed to load: {e}")
                self._stt = None
        try:
            self._config_mtime = get_config_path().stat().st_mtime
        except OSError:
            self._config_mtime = 0.0

    def _watch_config_changes(self) -> None:
        """Record the stt model size currently loaded."""
        if self._stt is not None:
            config = load_config()
            self._stt_model_size = config.stt.model_size

    def _maybe_reload_models(self) -> None:
        """Reload engines if config.json changed (e.g. whisper model switch)."""
        if not self._watch_config:
            return
        try:
            path = get_config_path()
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            config = load_config()
        except Exception:
            return

        if self._stt is not None and config.stt.model_size != self._stt_model_size:
            logger.info(f"STT model changed ({self._stt_model_size} → {config.stt.model_size}) — reloading")
            try:
                self._stt = self._stt_factory()
                self._stt_model_size = config.stt.model_size
            except Exception as e:
                logger.error(f"STT reload failed, keeping old model: {e}")

    # ─── Watchdog ───────────────────────────────────────────────────────────

    def _watchdog(self) -> None:
        while not self._exit.wait(_CONFIG_CHECK_INTERVAL):
            self._maybe_reload_models()
            if (self._idle_exit_s is not None
                    and time.time() - self._last_request > self._idle_exit_s):
                logger.info("Engine service idle — exiting (next request respawns it)")
                if self._server is not None:
                    self._server.shutdown()
                # Note: NOT os._exit here — serve_forever's finally cleans the socket.
                return

    # ─── Request handling ───────────────────────────────────────────────────

    def _handle_connection(self, conn: socket.socket) -> None:
        """Handle one framed request. Frame: [4B json_len][json][payload]."""
        try:
            req, payload = self._read_request(conn)
            resp, resp_payload = self._dispatch(req, payload)
            self._write_response(conn, resp, resp_payload)
        except (ConnectionError, OSError) as e:
            logger.debug(f"client connection ended: {e}")
        except Exception as e:
            logger.error(f"request failed: {e}")
            try:
                self._write_response(conn, {"ok": False, "error": str(e)}, b"")
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(conn: socket.socket) -> tuple[dict, bytes]:
        json_len = _HEADER.unpack(_recv_exact(conn, 4))[0]
        if json_len == 0 or json_len > _MAX_JSON_BYTES:
            raise ValueError(f"bad json length: {json_len}")
        req = json.loads(_recv_exact(conn, json_len))
        payload_len = int(req.get("payload_len", 0))
        if payload_len < 0 or payload_len > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"bad payload length: {payload_len}")
        payload = _recv_exact(conn, payload_len) if payload_len else b""
        return req, payload

    @staticmethod
    def _write_response(conn: socket.socket, resp: dict, payload: bytes) -> None:
        resp = dict(resp)
        resp["payload_len"] = len(payload)
        header = json.dumps(resp).encode()
        conn.sendall(_HEADER.pack(len(header)) + header + payload)

    def _dispatch(self, req: dict, payload: bytes) -> tuple[dict, bytes]:
        self._last_request = time.time()
        self._requests += 1

        op = req.get("op")
        if op == "synthesize":
            return self._op_synthesize(req)
        if op == "transcribe":
            return self._op_transcribe(req, payload)
        if op == "status":
            return self._op_status(), b""
        if op == "shutdown":
            # Respond first; exit after the connection closes.
            threading.Thread(target=self._graceful_shutdown, daemon=True).start()
            return {"ok": True}, b""
        return {"ok": False, "error": f"unknown op: {op}"}, b""

    def _op_synthesize(self, req: dict) -> tuple[dict, bytes]:
        if self._tts is None:
            return {"ok": False, "error": "tts_unavailable"}, b""
        result = self._tts.synthesize(req.get("text", ""), req.get("voice", ""), float(req.get("speed", 1.0)))
        samples = np.ascontiguousarray(result.samples, dtype=np.float32)
        return (
            {
                "ok": True,
                "sample_rate": result.sample_rate,
                "duration_ms": round(result.duration_ms, 1),
                "synthesis_ms": round(result.synthesis_ms, 1),
            },
            samples.tobytes(),
        )

    def _op_transcribe(self, req: dict, payload: bytes) -> tuple[dict, bytes]:
        if self._stt is None:
            return {"ok": False, "error": "stt_unavailable"}, b""
        if not payload:
            return {"ok": False, "error": "empty_audio"}, b""
        audio = np.frombuffer(payload, dtype=np.float32).copy()
        result = self._stt.transcribe(audio, int(req.get("sample_rate", 16000)))
        return (
            {
                "ok": True,
                "text": result.text,
                "confidence": round(result.confidence, 3),
                "transcription_ms": round(result.transcription_ms, 1),
                "language": result.language,
            },
            b"",
        )

    def _op_status(self) -> dict:
        return {
            "ok": True,
            "pid": os.getpid(),
            "tts_loaded": self._tts is not None,
            "stt_loaded": self._stt is not None,
            "stt_model_size": self._stt_model_size,
            "uptime_s": round(time.time() - self._started),
            "requests": self._requests,
        }

    def _graceful_shutdown(self) -> None:
        time.sleep(0.2)  # let the response flush
        if self._server is not None:
            self._server.shutdown()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()

    idle_min = getattr(config.engine, "idle_exit_min", 30.0)
    idle_exit_s = (idle_min * 60) if idle_min and idle_min > 0 else None

    def tts_factory():
        from tts.kokoro_engine import KokoroEngine
        return KokoroEngine(config.tts.model_path, config.tts.voices_path)

    def stt_factory():
        from stt.whisper_engine import WhisperEngine
        return WhisperEngine(config.stt.model_size, config.stt.language)

    service = EngineService(
        idle_exit_s=idle_exit_s,
        watch_config=True,
        tts_factory=tts_factory,
        stt_factory=stt_factory,
    )
    if not service.start():
        return  # another instance owns the socket
    service.serve_forever()


if __name__ == "__main__":
    # Bypass interpreter finalization: onnxruntime/ctranslate2 native thread
    # pools abort Py_FinalizeEx (same reason as server.py's _exit_process).
    code = 0
    try:
        main()
    except BaseException as e:
        logger.exception(f"Engine service crashed: {e}")
        code = 1
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)