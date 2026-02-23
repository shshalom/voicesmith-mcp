"""
Agent Voice MCP Server

A Model Context Protocol server providing local TTS (Kokoro ONNX) and STT
(faster-whisper) capabilities. Runs over stdio transport.

Usage:
    python server.py          # Normal MCP server mode
    python server.py --test   # Quick smoke test
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from shared import (
    ALL_VOICE_IDS,
    VOICE_METADATA,
    SAMPLE_RATE,
    STT_SAMPLE_RATE,
    SILENCE_THRESHOLD,
    LISTEN_TIMEOUT,
    REGISTRY_SAVE_INTERVAL,
    DEFAULT_HTTP_PORT,
    SpeakResult,
    ListenResult,
    TTSEngineError,
    STTEngineError,
    VADError,
    get_logger,
)
from config import load_config, save_config, get_config_path, AppConfig
from session_registry import register_session, unregister_session

logger = get_logger("server")

# ─── Global State ─────────────────────────────────────────────────────────────

mcp = FastMCP("agent-voice")

# Engine instances (initialized at startup)
_tts_engine = None
_audio_player = None
_speech_queue = None
_stt_engine = None
_vad = None
_mic_capture = None
_registry = None
_config: AppConfig = None

# State flags
_muted = False
_listen_cancel_event: asyncio.Event = None
_listen_active = False
_startup_time = time.time()
_session_info: dict = None
_event_loop: asyncio.AbstractEventLoop = None
_wake_listener = None


# ─── Startup / Shutdown ──────────────────────────────────────────────────────

def _init_tts(config: AppConfig):
    """Initialize TTS engine, audio player, and speech queue."""
    global _tts_engine, _audio_player, _speech_queue

    from tts.kokoro_engine import KokoroEngine
    from tts.audio_player import AudioPlayer
    from tts.speech_queue import SpeechQueue

    try:
        _tts_engine = KokoroEngine(config.tts.model_path, config.tts.voices_path)
        _audio_player = AudioPlayer(config.tts.audio_player)
        _speech_queue = SpeechQueue(_tts_engine, _audio_player)
        logger.info("TTS subsystem initialized")
    except TTSEngineError as e:
        logger.error(f"TTS initialization failed: {e}")
        _tts_engine = None


def _init_stt(config: AppConfig):
    """Initialize STT engine, VAD, and mic capture."""
    global _stt_engine, _vad, _mic_capture

    from stt.whisper_engine import WhisperEngine
    from stt.vad import VoiceActivityDetector
    from stt.mic_capture import MicCapture

    try:
        _stt_engine = WhisperEngine(config.stt.model_size, config.stt.language)
    except STTEngineError as e:
        logger.error(f"STT initialization failed: {e}")
        _stt_engine = None

    try:
        _vad = VoiceActivityDetector()
    except VADError as e:
        logger.warning(f"VAD initialization failed: {e}")
        _vad = None

    if _stt_engine is not None:
        _mic_capture = MicCapture(STT_SAMPLE_RATE)
        logger.info("STT subsystem initialized")


def _init_registry(config: AppConfig):
    """Initialize voice registry."""
    global _registry

    from voice_registry import VoiceRegistry

    config_path = get_config_path()
    _registry = VoiceRegistry(
        config_path=config_path if config_path.exists() else None,
        preloaded_registry=config.voice_registry or None,
        default_voice=config.tts.default_voice,
    )
    logger.info(f"Voice registry initialized ({_registry.size} entries)")


def _init_wake(config: AppConfig):
    """Initialize wake word listener if enabled."""
    global _wake_listener

    if not config.wake_word.enabled:
        logger.info("Wake word listener disabled")
        return

    if _stt_engine is None or _vad is None:
        logger.warning("Cannot start wake listener: STT or VAD not loaded")
        return

    try:
        from wake_listener import WakeWordListener

        tmux_session = os.environ.get("AGENT_VOICE_TMUX")
        _wake_listener = WakeWordListener(
            stt_engine=_stt_engine,
            vad=_vad,
            wake_model_name=config.wake_word.model,
            threshold=config.wake_word.threshold,
            tmux_session=tmux_session,
            ready_sound=config.wake_word.ready_sound,
            recording_timeout=config.wake_word.recording_timeout,
            no_speech_timeout=config.wake_word.no_speech_timeout,
        )
        _wake_listener.start()
        logger.info("Wake word listener initialized and started")
    except ImportError:
        logger.info("openWakeWord not installed — wake word feature unavailable")
    except Exception as e:
        logger.warning(f"Failed to initialize wake listener: {e}")


# ─── HTTP Listener (Push-to-Talk) ─────────────────────────────────────────────

class _VoiceHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for push-to-talk. Runs in a daemon thread."""

    def do_GET(self):
        if self.path == "/status":
            body = json.dumps({
                "ready": True,
                "name": _session_info.get("name") if _session_info else None,
                "port": _session_info.get("port") if _session_info else None,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/listen":
            self._handle_listen()
        else:
            self.send_error(404)

    def _handle_listen(self):
        """Record mic → transcribe → return JSON. Bridges to async via the main event loop."""
        if _event_loop is None:
            self._json_response(500, {"error": "server_not_ready"})
            return

        # Schedule the listen coroutine on the main event loop
        future = asyncio.run_coroutine_threadsafe(
            listen(timeout=15, prompt="push-to-talk", silence_threshold=1.5),
            _event_loop,
        )
        try:
            result = future.result(timeout=30)  # Wait up to 30s
            self._json_response(200, result)
        except Exception as e:
            self._json_response(500, {"error": "listen_failed", "message": str(e)})

    def _json_response(self, code, data):
        body = json.dumps(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        """Redirect HTTP logs to our logger instead of stderr."""
        logger.debug(f"HTTP: {format % args}")


def _start_http_listener(port: int):
    """Start the HTTP listener in a daemon thread."""
    try:
        server = HTTPServer(("127.0.0.1", port), _VoiceHTTPHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"HTTP listener started on 127.0.0.1:{port}")
    except OSError as e:
        logger.warning(f"Failed to start HTTP listener on port {port}: {e}")


# ─── Shutdown ─────────────────────────────────────────────────────────────────

def _shutdown():
    """Graceful shutdown: stop playback, save registry, unregister session."""
    logger.info("Shutting down...")

    if _speech_queue is not None:
        _speech_queue.stop()

    if _mic_capture is not None:
        _mic_capture.stop()

    if _registry is not None:
        try:
            config_path = get_config_path()
            _registry.save(config_path)
            logger.info("Registry saved on shutdown")
        except Exception as e:
            logger.error(f"Failed to save registry on shutdown: {e}")

    if _wake_listener is not None:
        try:
            _wake_listener.stop()
        except Exception as e:
            logger.error(f"Failed to stop wake listener: {e}")

    try:
        unregister_session()
    except Exception as e:
        logger.error(f"Failed to unregister session: {e}")


# ─── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
async def speak(name: str, text: str, speed: float = 1.0, block: bool = True) -> dict:
    """Synthesize and play speech for a named agent.

    Args:
        name: Agent name (e.g., "Eric", "Nova"). Maps to a voice via the registry.
        text: The text to speak.
        speed: Speech speed multiplier (default 1.0).
        block: Whether to wait for playback to complete (default true).
    """
    _capture_event_loop()

    if _tts_engine is None or _speech_queue is None:
        return {"success": False, "error": "tts_unavailable", "message": "TTS engine not loaded"}

    if _muted:
        voice_id, auto_assigned = _registry.get_voice(name)
        if block:
            return {"success": True, "voice": voice_id, "auto_assigned": auto_assigned,
                    "duration_ms": 0, "synthesis_ms": 0, "muted": True}
        else:
            return {"success": True, "voice": voice_id, "auto_assigned": auto_assigned,
                    "queued": True, "muted": True}

    voice_id, auto_assigned = _registry.get_voice(name)

    # Pause wake listener during TTS to prevent it hearing our own speech
    wake_was_listening = _wake_listener is not None and _wake_listener.is_listening
    if wake_was_listening and block:
        _wake_listener.yield_mic()

    try:
        result = await _speech_queue.speak(text, voice_id, speed, block)

        if not block:
            return {
                "success": result.success,
                "voice": voice_id,
                "auto_assigned": auto_assigned,
                "queued": True,
            }

        response = {
            "success": result.success,
            "voice": voice_id,
            "auto_assigned": auto_assigned,
            "duration_ms": round(result.duration_ms, 1),
            "synthesis_ms": round(result.synthesis_ms, 1),
        }
    except Exception as e:
        logger.error(f"speak failed: {e}")
        response = {"success": False, "error": "speak_failed", "message": str(e)}

    # Resume wake listener after TTS
    if wake_was_listening and block and _wake_listener is not None:
        _wake_listener.reclaim_mic()

    return response


@mcp.tool()
async def listen(timeout: float = 15, prompt: str = "", silence_threshold: float = 1.5) -> dict:
    """Activate the microphone, record speech, and return transcribed text.

    Args:
        timeout: Maximum seconds to wait for speech (default 15).
        prompt: Optional context about what the AI is asking.
        silence_threshold: Seconds of silence before stopping (default 1.5).
    """
    global _listen_active, _listen_cancel_event

    if _stt_engine is None or _mic_capture is None:
        return {"success": False, "error": "stt_unavailable", "message": "STT engine not loaded"}

    if _muted:
        return {"success": False, "error": "muted", "message": "Voice input is muted"}

    if _listen_active:
        return {"success": False, "error": "mic_busy", "message": "Another listen call is in progress"}

    # Yield mic from wake listener if active
    if _wake_listener is not None and _wake_listener.is_listening:
        _wake_listener.yield_mic()

    _listen_active = True
    _listen_cancel_event = asyncio.Event()

    if prompt:
        logger.info(f"Listening (prompt: {prompt})")

    try:
        start = time.perf_counter()

        # Record audio with VAD
        audio = await _mic_capture.record(
            vad=_vad,
            timeout=timeout,
            silence_threshold=silence_threshold,
            cancel_event=_listen_cancel_event,
        )

        if _listen_cancel_event.is_set():
            return {"success": False, "cancelled": True}

        if audio is None:
            return {"success": False, "error": "timeout", "message": "No speech detected within timeout"}

        recording_ms = (time.perf_counter() - start) * 1000

        # Transcribe
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _stt_engine.transcribe, audio, STT_SAMPLE_RATE
        )

        total_ms = (time.perf_counter() - start) * 1000

        return {
            "success": True,
            "text": result.text,
            "confidence": round(result.confidence, 3),
            "duration_ms": round(total_ms, 1),
            "transcription_ms": round(result.transcription_ms, 1),
        }
    except Exception as e:
        logger.error(f"listen failed: {e}")
        return {"success": False, "error": "listen_failed", "message": str(e)}
    finally:
        _listen_active = False
        _listen_cancel_event = None
        # Reclaim mic for wake listener
        if _wake_listener is not None:
            _wake_listener.reclaim_mic()


@mcp.tool()
async def speak_then_listen(
    name: str,
    text: str,
    speed: float = 1.0,
    timeout: float = 15,
    silence_threshold: float = 1.5,
) -> dict:
    """Speak a question and immediately listen for the answer in one atomic call.

    Args:
        name: Agent name for the voice.
        text: The question to speak.
        speed: Speech speed (default 1.0).
        timeout: Max seconds to wait for response (default 15).
        silence_threshold: Seconds of silence before stopping (default 1.5).
    """
    speak_result = await speak(name, text, speed, block=True)

    if not speak_result.get("success"):
        return {"speak": speak_result, "listen": {"success": False, "error": "skipped"}}

    listen_result = await listen(timeout=timeout, silence_threshold=silence_threshold)

    return {"speak": speak_result, "listen": listen_result}


@mcp.tool()
async def list_voices() -> dict:
    """List all available Kokoro voices."""
    return {
        "voices": VOICE_METADATA,
        "total": len(VOICE_METADATA),
    }


@mcp.tool()
async def get_voice_registry() -> dict:
    """Get current agent-to-voice mappings."""
    if _registry is None:
        return {"error": "Registry not initialized"}

    registry = _registry.get_registry()
    pool = _registry.get_available_pool()
    return {
        "registry": registry,
        "available_pool": pool,
        "total_assigned": len(registry),
        "total_available": len(pool),
    }


@mcp.tool()
async def set_voice(name: str, voice: str) -> dict:
    """Assign or reassign a voice to an agent name.

    Args:
        name: Agent name to assign.
        voice: Kokoro voice ID (e.g., "am_eric"). Must be valid.
    """
    if _registry is None:
        return {"success": False, "error": "registry_unavailable"}

    if voice not in ALL_VOICE_IDS:
        return {
            "success": False,
            "error": "invalid_voice",
            "message": f"Voice '{voice}' not found. Use list_voices to see available options.",
        }

    _registry.set_voice(name, voice)
    return {"success": True, "name": name, "voice": voice}


@mcp.tool()
async def stop() -> dict:
    """Stop any currently playing audio and cancel any active listen recording."""
    stopped_playback = False
    cancelled_listen = False

    if _speech_queue is not None:
        stopped_playback = _speech_queue.stop()

    if _listen_cancel_event is not None:
        _listen_cancel_event.set()
        cancelled_listen = True

    if _mic_capture is not None and _mic_capture.is_recording:
        _mic_capture.stop()
        cancelled_listen = True

    return {
        "success": True,
        "stopped_playback": stopped_playback,
        "cancelled_listen": cancelled_listen,
    }


@mcp.tool(name="mute")
async def mute_tool() -> dict:
    """Temporarily silence all voice output. Speak still returns success but no audio plays."""
    global _muted
    _muted = True
    logger.info("Voice muted")
    return {"success": True, "muted": True}


@mcp.tool(name="unmute")
async def unmute_tool() -> dict:
    """Resume voice output after muting."""
    global _muted
    _muted = False
    logger.info("Voice unmuted")
    return {"success": True, "muted": False}


@mcp.tool()
async def wake_enable() -> dict:
    """Start the wake word listener for user-initiated voice input."""
    global _wake_listener
    if _wake_listener is not None and _wake_listener.is_listening:
        return {"success": True, "already_listening": True}

    if _wake_listener is None:
        # Try to initialize
        _init_wake(_config)

    if _wake_listener is not None:
        _wake_listener.start()
        return {"success": True, "wake_word": _config.wake_word.model, "listening": True}

    return {"success": False, "error": "wake_unavailable",
            "message": "openWakeWord not installed. Install with: --with-voice-wake"}


@mcp.tool()
async def wake_disable() -> dict:
    """Stop the wake word listener and release the microphone."""
    if _wake_listener is not None:
        _wake_listener.stop()
        return {"success": True, "listening": False}
    return {"success": True, "listening": False, "was_disabled": True}


@mcp.tool()
async def status() -> dict:
    """Report server health and component status."""
    uptime_s = round(time.time() - _startup_time)

    result = {
        "tts": {
            "loaded": _tts_engine is not None and _tts_engine.is_loaded(),
            "model": "kokoro-v1.0.onnx" if _tts_engine else None,
            "voices": len(ALL_VOICE_IDS) if _tts_engine else 0,
        },
        "stt": {
            "loaded": _stt_engine is not None and _stt_engine.is_loaded(),
            "model": f"whisper-{_config.stt.model_size}" if _stt_engine and _config else None,
            "language": _config.stt.language if _config else None,
        },
        "vad": {
            "loaded": _vad is not None and _vad.is_loaded(),
        },
        "muted": _muted,
        "uptime_s": uptime_s,
        "registry_size": _registry.size if _registry else 0,
        "queue_depth": _speech_queue.depth if _speech_queue else 0,
        "session": _session_info,
        "wake_word": {
            "enabled": _config.wake_word.enabled if _config else False,
            "listening": _wake_listener.is_listening if _wake_listener else False,
            "state": _wake_listener.state if _wake_listener else "disabled",
            "model": _config.wake_word.model if _config else None,
            "tmux_session": _session_info.get("tmux_session") if _session_info else None,
        },
    }
    return result


# ─── Server Lifecycle ─────────────────────────────────────────────────────────

def _run_smoke_test():
    """Quick smoke test: load engines, synthesize one sentence."""
    logger.info("Running smoke test...")
    config = load_config()
    _init_tts(config)
    _init_stt(config)
    _init_registry(config)

    results = []

    if _tts_engine and _tts_engine.is_loaded():
        logger.info("TTS: OK")
        results.append("TTS: OK")
    else:
        logger.error("TTS: FAILED")
        results.append("TTS: FAILED")

    if _stt_engine and _stt_engine.is_loaded():
        logger.info("STT: OK")
        results.append("STT: OK")
    else:
        logger.error("STT: FAILED")
        results.append("STT: FAILED")

    if _vad and _vad.is_loaded():
        logger.info("VAD: OK")
        results.append("VAD: OK")
    else:
        logger.warning("VAD: FAILED")
        results.append("VAD: FAILED")

    if _registry:
        voice_id, auto = _registry.get_voice("Eric")
        logger.info(f"Registry: OK (Eric -> {voice_id})")
        results.append(f"Registry: OK (Eric -> {voice_id})")

    # Try synthesis if TTS is available
    if _tts_engine and _tts_engine.is_loaded():
        try:
            result = _tts_engine.synthesize("Hello, this is a smoke test.", "am_eric", 1.0)
            logger.info(f"Synthesis: OK ({result.duration_ms:.0f}ms audio in {result.synthesis_ms:.0f}ms)")
            results.append("Synthesis: OK")

            if _audio_player:
                playback = _audio_player.play(result.samples, result.sample_rate)
                if playback.success:
                    logger.info("Playback: OK")
                    results.append("Playback: OK")
                else:
                    logger.error(f"Playback: FAILED ({playback.error})")
                    results.append("Playback: FAILED")
        except Exception as e:
            logger.error(f"Synthesis: FAILED ({e})")
            results.append("Synthesis: FAILED")

    print("\n".join(results), file=sys.stderr)


def main():
    """Entry point."""
    global _config, _listen_cancel_event, _session_info, _event_loop

    if "--test" in sys.argv:
        _run_smoke_test()
        return

    # Load configuration
    _config = load_config()

    # Set log level
    log_level = getattr(__import__("logging"), _config.log_level.upper(), None)
    if log_level:
        logger.setLevel(log_level)

    logger.info("Starting Agent Voice MCP Server...")

    # Initialize subsystems
    _init_tts(_config)
    _init_stt(_config)
    _init_registry(_config)
    _init_wake(_config)

    # Check if at least one engine loaded
    tts_ok = _tts_engine is not None and _tts_engine.is_loaded()
    stt_ok = _stt_engine is not None and _stt_engine.is_loaded()

    if not tts_ok and not stt_ok:
        logger.error("Both TTS and STT failed to load. Cannot start server.")
        logger.error(f"TTS model path: {_config.tts.model_path}")
        logger.error(f"STT model size: {_config.stt.model_size}")
        sys.exit(1)

    if not tts_ok:
        logger.warning("TTS failed to load. Running with STT only.")
    if not stt_ok:
        logger.warning("STT failed to load. Running with TTS only.")

    logger.info(f"Server ready (TTS: {'OK' if tts_ok else 'FAILED'}, STT: {'OK' if stt_ok else 'FAILED'})")

    # Register session for multi-session coordination
    _session_info = register_session(
        preferred_name=_config.main_agent,
        preferred_voice=_config.tts.default_voice,
        base_port=_config.http_port,
    )

    # Start HTTP listener for push-to-talk
    _start_http_listener(_session_info["port"])

    # Register shutdown handlers
    def handle_signal(signum, frame):
        _shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    import atexit
    atexit.register(_shutdown)

    # Store event loop reference for HTTP→async bridge
    # FastMCP creates the loop internally; we capture it via a startup task
    _start_periodic_save_thread()
    # Event loop is captured on first MCP tool call via _capture_event_loop()
    mcp.run(transport="stdio")


def _capture_event_loop():
    """Capture the asyncio event loop from within an async context.

    Called on the first MCP tool invocation to grab the running loop
    for use by the HTTP listener's run_coroutine_threadsafe().
    """
    global _event_loop
    if _event_loop is None:
        try:
            _event_loop = asyncio.get_running_loop()
            logger.info(f"Captured asyncio event loop for HTTP bridge")
        except RuntimeError:
            pass


def _start_periodic_save_thread():
    """Start a daemon thread that periodically saves the registry."""
    import threading

    def _save_loop():
        while True:
            time.sleep(REGISTRY_SAVE_INTERVAL)
            if _registry is not None:
                try:
                    config_path = get_config_path()
                    _registry.save(config_path)
                    logger.debug("Periodic registry save completed")
                except Exception as e:
                    logger.error(f"Periodic registry save failed: {e}")


if __name__ == "__main__":
    main()
