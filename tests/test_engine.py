"""Tests for the shared engine service (model host) and its thin clients."""

import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import SynthesisResult, TranscriptionResult
from engine_service import EngineService
from engine_client import (
    EngineServiceError,
    RemoteTTSEngine,
    RemoteSTTEngine,
    connect,
    request,
    service_status,
    spawn_service,
)


def _short_paths() -> tuple[str, str]:
    """Short AF_UNIX socket + lock paths (macOS sun_path limit ~104 chars).

    pytest's tmp_path is too deeply nested for AF_UNIX sockets, so use
    /tmp with a unique suffix and clean up after.
    """
    tag = uuid.uuid4().hex[:8]
    return f"/tmp/voicesmith-test-{tag}.sock", f"/tmp/voicesmith-test-{tag}.lock"


# ─── Fake engines (no model loading) ──────────────────────────────────────────


class FakeTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice_id, speed=1.0):
        self.calls.append((text, voice_id, speed))
        samples = (np.arange(240, dtype=np.float32) / 1000.0) * speed  # 10ms of 24k
        return SynthesisResult(
            samples=samples, sample_rate=24000,
            duration_ms=10.0, synthesis_ms=1.5,
        )


class FakeSTT:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, sample_rate=16000):
        self.calls.append((len(audio), sample_rate))
        return TranscriptionResult(
            text="hello world", confidence=0.9,
            transcription_ms=2.0, language="en",
        )


@pytest.fixture
def fake_engine():
    """A running EngineService on short /tmp paths with fake models."""
    socket_path, lock_path = _short_paths()
    service = EngineService(
        socket_path=socket_path,
        lock_path=lock_path,
        tts_factory=FakeTTS,
        stt_factory=FakeSTT,
        idle_exit_s=None,
        watch_config=False,
    )
    assert service.start() is True

    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    # Give serve_forever a moment to enter its poll loop
    time.sleep(0.1)

    yield service

    if service._server is not None:
        service._server.shutdown()
    for p in (socket_path, lock_path):
        try:
            os.unlink(p)
        except OSError:
            pass


def _socket_path(fake_engine) -> str:
    return fake_engine._socket_path


# ─── Protocol tests ────────────────────────────────────────────────────────────


class TestEngineServiceProtocol:
    def test_synthesize_round_trip(self, fake_engine):
        engine = RemoteTTSEngine(socket_path=_socket_path(fake_engine))
        result = engine.synthesize("Hello there", "am_eric", speed=1.5)

        assert result.sample_rate == 24000
        assert len(result.samples) == 240
        assert result.samples.dtype == np.float32
        # speed multiplier flowed through to the fake engine
        assert result.samples[10] == pytest.approx(0.01 * 1.5, abs=1e-6)
        assert fake_engine._tts.calls == [("Hello there", "am_eric", 1.5)]
        assert result.synthesis_ms == 1.5

    def test_transcribe_round_trip(self, fake_engine):
        engine = RemoteSTTEngine(socket_path=_socket_path(fake_engine))
        audio = np.ones(16000, dtype=np.float32)
        result = engine.transcribe(audio, 16000)

        assert result.text == "hello world"
        assert result.confidence == 0.9
        assert result.language == "en"
        assert fake_engine._stt.calls == [(16000, 16000)]

    def test_status_op(self, fake_engine):
        status = service_status(socket_path=_socket_path(fake_engine))
        assert status["running"] is True
        assert status["tts_loaded"] is True
        assert status["stt_loaded"] is True
        assert status["requests"] >= 1

    def test_unknown_op_raises(self, fake_engine):
        with pytest.raises(EngineServiceError):
            request("bogus", socket_path=_socket_path(fake_engine))

    def test_status_when_not_running(self):
        status = service_status(socket_path="/tmp/voicesmith-test-missing.sock")
        assert status == {"running": False}

    def test_connect_spawn_disabled_raises(self):
        with pytest.raises(OSError):
            connect(spawn=False, socket_path="/tmp/voicesmith-test-missing.sock",
                    startup_timeout=0.1)


class TestSingleInstanceGuard:
    def test_second_instance_refuses_to_start(self):
        socket_path, lock_path = _short_paths()
        first = EngineService(
            socket_path=socket_path,
            lock_path=lock_path,
            tts_factory=FakeTTS,
            stt_factory=FakeSTT,
            idle_exit_s=None,
            watch_config=False,
        )
        assert first.start() is True

        second = EngineService(
            socket_path=socket_path,
            lock_path=lock_path,
            tts_factory=FakeTTS,
            stt_factory=FakeSTT,
            idle_exit_s=None,
            watch_config=False,
        )
        assert second.start() is False  # yields to the running instance

        # No serve_forever loop is running for `first`, so use _cleanup
        # (shutdown() would block waiting for a loop that doesn't exist).
        first._cleanup()
        for p in (socket_path, lock_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ─── Client spawn behavior ────────────────────────────────────────────────────


class TestSpawnBehavior:
    def test_connect_waits_for_service_to_appear(self, monkeypatch):
        """connect(spawn=True) polls until the socket shows up."""
        socket_path, lock_path = _short_paths()
        monkeypatch.setattr("engine_client.spawn_service", lambda: None)

        def late_start():
            time.sleep(0.4)
            svc = EngineService(
                socket_path=socket_path,
                lock_path=lock_path,
                tts_factory=FakeTTS,
                stt_factory=FakeSTT,
                idle_exit_s=None,
                watch_config=False,
            )
            assert svc.start() is True

        threading.Thread(target=late_start, daemon=True).start()

        sock = connect(spawn=True, socket_path=socket_path, startup_timeout=5.0)
        sock.close()
        # Let the late service shut down and clean its socket
        time.sleep(0.2)

    def test_spawn_timeout_raises(self, monkeypatch):
        monkeypatch.setattr("engine_client.spawn_service", lambda: None)
        with pytest.raises(EngineServiceError):
            connect(spawn=True, socket_path="/tmp/voicesmith-test-missing.sock",
                    startup_timeout=0.5)


# ─── Server integration ───────────────────────────────────────────────────────


class TestServerIntegration:
    def test_init_tts_remote_when_enabled(self, tmp_path):
        import server
        from config import AppConfig

        config = AppConfig()
        config.engine.enabled = True
        server._init_tts(config)

        from engine_client import RemoteTTSEngine
        assert isinstance(server._tts_engine, RemoteTTSEngine)
        assert server._tts_engine.is_loaded() is True
        assert server._speech_queue is not None

        # Remote engine must NOT have loaded models — client-only process
        assert not hasattr(server._tts_engine, "_model")

    def test_init_stt_remote_when_enabled(self):
        import server
        from config import AppConfig
        from engine_client import RemoteSTTEngine

        config = AppConfig()
        config.engine.enabled = True
        server._init_stt(config)

        assert isinstance(server._stt_engine, RemoteSTTEngine)
        assert server._stt_engine.is_loaded() is True
        # Mic capture is created whenever the STT engine exists (VAD may
        # be None if the Silero model isn't present in the test env).
        assert server._mic_capture is not None

    def test_init_tts_local_when_disabled(self, tmp_path):
        import server
        from config import AppConfig

        config = AppConfig()
        config.engine.enabled = False
        # Point at a nonexistent model so KokoroEngine fails cleanly
        config.tts.model_path = str(tmp_path / "nope.onnx")
        config.tts.voices_path = str(tmp_path / "nope.bin")
        server._init_tts(config)

        assert server._tts_engine is None  # load failed, same as before

    def test_engine_service_status_disabled(self):
        import server
        from config import AppConfig

        saved = server._config
        try:
            server._config = AppConfig()
            server._config.engine.enabled = False
            assert server._engine_service_status() == {"enabled": False}
        finally:
            server._config = saved