"""Shared pytest fixtures for Agent Voice MCP Server tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import (
    SynthesisResult,
    PlaybackResult,
    SpeakResult,
    ListenResult,
    TranscriptionResult,
    SAMPLE_RATE,
    STT_SAMPLE_RATE,
)


# ─── TTS Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_kokoro_engine():
    """A mocked KokoroEngine that returns valid SynthesisResult."""
    engine = MagicMock()
    engine.is_loaded.return_value = True

    samples = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1 second
    engine.synthesize.return_value = SynthesisResult(
        samples=samples,
        sample_rate=SAMPLE_RATE,
        duration_ms=1000.0,
        synthesis_ms=100.0,
    )
    return engine


@pytest.fixture
def mock_audio_player():
    """A mocked AudioPlayer that succeeds."""
    player = MagicMock()
    player.is_playing = False
    player.play.return_value = PlaybackResult(success=True, duration_ms=1000.0)
    player.stop.return_value = False
    return player


@pytest.fixture
def mock_speech_queue(mock_kokoro_engine, mock_audio_player):
    """A mocked SpeechQueue."""
    queue = MagicMock()
    queue.depth = 0

    async def mock_speak(text, voice_id, speed=1.0, block=True):
        if block:
            return SpeakResult(
                success=True, voice=voice_id,
                duration_ms=1000.0, synthesis_ms=100.0,
            )
        return SpeakResult(success=True, voice=voice_id, queued=True)

    queue.speak = mock_speak
    queue.stop.return_value = False
    return queue


# ─── STT Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_whisper_engine():
    """A mocked WhisperEngine that returns valid TranscriptionResult."""
    engine = MagicMock()
    engine.is_loaded.return_value = True
    engine.transcribe.return_value = TranscriptionResult(
        text="hello world",
        confidence=0.95,
        transcription_ms=200.0,
        language="en",
    )
    return engine


@pytest.fixture
def mock_vad():
    """A mocked VoiceActivityDetector."""
    vad = MagicMock()
    vad.is_loaded.return_value = True
    vad.is_speech.return_value = False
    vad.speech_probability.return_value = 0.1
    return vad


@pytest.fixture
def mock_mic_capture():
    """A mocked MicCapture."""
    mic = MagicMock()
    mic.is_recording = False
    return mic


# ─── Registry Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def mock_registry():
    """A mocked VoiceRegistry."""
    from voice_registry import VoiceRegistry
    return VoiceRegistry(default_voice="am_eric")


# ─── Config Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_config():
    """A default AppConfig for testing."""
    from config import AppConfig
    return AppConfig()
