"""Integration tests for the MCP server tool handlers."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import (
    SynthesisResult,
    PlaybackResult,
    SpeakResult,
    TranscriptionResult,
    SAMPLE_RATE,
    STT_SAMPLE_RATE,
    ALL_VOICE_IDS,
    VOICE_METADATA,
)
from voice_registry import VoiceRegistry


def _setup_server_globals(
    tts_engine=None,
    audio_player=None,
    speech_queue=None,
    stt_engine=None,
    vad=None,
    mic_capture=None,
    registry=None,
    muted=False,
):
    """Set up server module globals for testing."""
    import server

    server._tts_engine = tts_engine
    server._audio_player = audio_player
    server._speech_queue = speech_queue
    server._stt_engine = stt_engine
    server._vad = vad
    server._mic_capture = mic_capture
    server._registry = registry or VoiceRegistry()
    server._muted = muted
    server._listen_active = False
    server._listen_cancel_event = None
    server._config = MagicMock()
    server._config.stt.model_size = "base"
    server._config.stt.language = "en"


def _mock_tts():
    """Return mocked TTS components."""
    engine = MagicMock()
    engine.is_loaded.return_value = True

    player = MagicMock()
    player.is_playing = False
    player.play.return_value = PlaybackResult(success=True, duration_ms=1000.0)
    player.stop.return_value = False

    queue = MagicMock()
    queue.depth = 0
    queue.stop.return_value = False

    async def mock_speak(text, voice_id, speed=1.0, block=True):
        if block:
            return SpeakResult(
                success=True, voice=voice_id,
                duration_ms=1000.0, synthesis_ms=100.0,
            )
        return SpeakResult(success=True, voice=voice_id, queued=True)

    queue.speak = mock_speak
    return engine, player, queue


def _mock_stt():
    """Return mocked STT components."""
    engine = MagicMock()
    engine.is_loaded.return_value = True
    engine.transcribe.return_value = TranscriptionResult(
        text="hello world", confidence=0.95,
        transcription_ms=200.0, language="en",
    )

    vad = MagicMock()
    vad.is_loaded.return_value = True

    mic = MagicMock()
    mic.is_recording = False
    mic.stop.return_value = None

    return engine, vad, mic


# ─── Speak Tool Tests ────────────────────────────────────────────────────────


class TestSpeakTool:
    @pytest.mark.asyncio
    async def test_speak_blocking(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.speak("Eric", "Hello world", speed=1.0, block=True)

        assert result["success"] is True
        assert result["voice"] == "am_eric"
        assert "duration_ms" in result

    @pytest.mark.asyncio
    async def test_speak_nonblocking(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.speak("Eric", "Hello world", block=False)

        assert result["success"] is True
        assert result["queued"] is True

    @pytest.mark.asyncio
    async def test_speak_when_tts_unavailable(self):
        _setup_server_globals()

        import server
        result = await server.speak("Eric", "Hello")

        assert result["success"] is False
        assert result["error"] == "tts_unavailable"

    @pytest.mark.asyncio
    async def test_speak_when_muted(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(
            tts_engine=engine, audio_player=player, speech_queue=queue, muted=True
        )

        import server
        result = await server.speak("Eric", "Hello", block=True)

        assert result["success"] is True
        assert result.get("muted") is True
        assert result["duration_ms"] == 0

    @pytest.mark.asyncio
    async def test_speak_auto_assigns_voice(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.speak("Eric", "Hello", block=True)
        assert result["auto_assigned"] is True  # First call auto-assigns

        result2 = await server.speak("Eric", "Hello again", block=True)
        assert result2["auto_assigned"] is False  # Second call uses existing


# ─── Listen Tool Tests ────────────────────────────────────────────────────────


class TestListenTool:
    @pytest.mark.asyncio
    async def test_listen_when_stt_unavailable(self):
        _setup_server_globals()

        import server
        result = await server.listen()

        assert result["success"] is False
        assert result["error"] == "stt_unavailable"

    @pytest.mark.asyncio
    async def test_listen_when_muted(self):
        stt_engine, vad, mic = _mock_stt()
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic, muted=True)

        import server
        result = await server.listen()

        assert result["success"] is False
        assert result["error"] == "muted"

    @pytest.mark.asyncio
    async def test_listen_concurrent_rejection(self):
        stt_engine, vad, mic = _mock_stt()
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic)

        import server
        server._listen_active = True

        result = await server.listen()
        assert result["success"] is False
        assert result["error"] == "mic_busy"

        server._listen_active = False

    @pytest.mark.asyncio
    async def test_listen_timeout(self):
        stt_engine, vad, mic = _mock_stt()

        async def mock_record(**kwargs):
            return None  # Timeout / no speech

        mic.record = mock_record
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic)

        import server
        result = await server.listen(timeout=1)

        assert result["success"] is False
        assert result["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_listen_successful_transcription(self):
        stt_engine, vad, mic = _mock_stt()
        audio = np.zeros(16000, dtype=np.float32)

        async def mock_record(**kwargs):
            return audio

        mic.record = mock_record
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic)

        import server
        result = await server.listen(timeout=5)

        assert result["success"] is True
        assert result["text"] == "hello world"
        assert result["confidence"] == 0.95


# ─── Stop Tool Tests ─────────────────────────────────────────────────────────


class TestStopTool:
    @pytest.mark.asyncio
    async def test_stop_playback(self):
        engine, player, queue = _mock_tts()
        queue.stop.return_value = True
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.stop()

        assert result["success"] is True
        assert result["stopped_playback"] is True

    @pytest.mark.asyncio
    async def test_stop_cancels_listen(self):
        stt_engine, vad, mic = _mock_stt()
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic)

        import server
        server._listen_cancel_event = asyncio.Event()
        result = await server.stop()

        assert result["cancelled_listen"] is True
        assert server._listen_cancel_event.is_set()


# ─── Mute/Unmute Tool Tests ──────────────────────────────────────────────────


class TestMuteUnmuteTool:
    @pytest.mark.asyncio
    async def test_mute(self):
        _setup_server_globals()

        import server
        result = await server.mute_tool()

        assert result["success"] is True
        assert result["muted"] is True
        assert server._muted is True

    @pytest.mark.asyncio
    async def test_unmute(self):
        _setup_server_globals(muted=True)

        import server
        result = await server.unmute_tool()

        assert result["success"] is True
        assert result["muted"] is False
        assert server._muted is False

    @pytest.mark.asyncio
    async def test_mute_unmute_cycle(self):
        _setup_server_globals()

        import server
        await server.mute_tool()
        assert server._muted is True

        await server.unmute_tool()
        assert server._muted is False


# ─── List Voices Tool Tests ──────────────────────────────────────────────────


class TestListVoicesTool:
    @pytest.mark.asyncio
    async def test_list_voices(self):
        import server
        result = await server.list_voices()

        assert "voices" in result
        assert result["total"] == 54
        assert len(result["voices"]) == 54


# ─── Voice Registry Tool Tests ───────────────────────────────────────────────


class TestVoiceRegistryTool:
    @pytest.mark.asyncio
    async def test_get_voice_registry(self):
        _setup_server_globals()

        import server
        result = await server.get_voice_registry()

        assert "registry" in result
        assert "available_pool" in result
        assert result["total_assigned"] == 0
        assert result["total_available"] == 54

    @pytest.mark.asyncio
    async def test_set_voice_valid(self):
        _setup_server_globals()

        import server
        result = await server.set_voice("TestAgent", "af_nova")

        assert result["success"] is True
        assert result["name"] == "TestAgent"
        assert result["voice"] == "af_nova"

    @pytest.mark.asyncio
    async def test_set_voice_invalid(self):
        _setup_server_globals()

        import server
        result = await server.set_voice("TestAgent", "invalid_voice")

        assert result["success"] is False
        assert result["error"] == "invalid_voice"


# ─── Status Tool Tests ───────────────────────────────────────────────────────


class TestStatusTool:
    @pytest.mark.asyncio
    async def test_status_all_loaded(self):
        engine, player, queue = _mock_tts()
        stt_engine, vad, mic = _mock_stt()
        _setup_server_globals(
            tts_engine=engine, audio_player=player, speech_queue=queue,
            stt_engine=stt_engine, vad=vad, mic_capture=mic,
        )

        import server
        result = await server.status()

        assert result["tts"]["loaded"] is True
        assert result["stt"]["loaded"] is True
        assert result["vad"]["loaded"] is True
        assert result["muted"] is False
        assert "uptime_s" in result

    @pytest.mark.asyncio
    async def test_status_tts_only(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.status()

        assert result["tts"]["loaded"] is True
        assert result["stt"]["loaded"] is False

    @pytest.mark.asyncio
    async def test_status_when_muted(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(
            tts_engine=engine, audio_player=player, speech_queue=queue, muted=True
        )

        import server
        result = await server.status()
        assert result["muted"] is True


# ─── Speak Then Listen Tool Tests ────────────────────────────────────────────


class TestSpeakThenListenTool:
    @pytest.mark.asyncio
    async def test_speak_then_listen_success(self):
        engine, player, queue = _mock_tts()
        stt_engine, vad, mic = _mock_stt()
        audio = np.zeros(16000, dtype=np.float32)

        async def mock_record(**kwargs):
            return audio

        mic.record = mock_record
        _setup_server_globals(
            tts_engine=engine, audio_player=player, speech_queue=queue,
            stt_engine=stt_engine, vad=vad, mic_capture=mic,
        )

        import server
        result = await server.speak_then_listen("Eric", "What do you think?")

        assert result["speak"]["success"] is True
        assert result["listen"]["success"] is True
        assert result["listen"]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_speak_then_listen_tts_failure(self):
        _setup_server_globals()

        import server
        result = await server.speak_then_listen("Eric", "Hello?")

        assert result["speak"]["success"] is False
        assert result["listen"]["error"] == "skipped"


# ─── Graceful Degradation Tests ──────────────────────────────────────────────


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_speak_works_without_stt(self):
        engine, player, queue = _mock_tts()
        _setup_server_globals(tts_engine=engine, audio_player=player, speech_queue=queue)

        import server
        result = await server.speak("Eric", "Hello", block=True)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_listen_works_without_tts(self):
        stt_engine, vad, mic = _mock_stt()
        audio = np.zeros(16000, dtype=np.float32)

        async def mock_record(**kwargs):
            return audio

        mic.record = mock_record
        _setup_server_globals(stt_engine=stt_engine, vad=vad, mic_capture=mic)

        import server
        result = await server.listen(timeout=5)
        assert result["success"] is True
