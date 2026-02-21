"""Tests for the TTS subsystem."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import (
    SynthesisResult,
    PlaybackResult,
    SpeakResult,
    TTSEngineError,
    AudioPlayerError,
    ALL_VOICE_IDS,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_kokoro_module():
    """Provide a mocked kokoro_onnx module with a working model."""
    mock_module = MagicMock()
    mock_model = MagicMock()
    samples = np.zeros(24000, dtype=np.float32)  # 1 second at 24kHz
    mock_model.create.return_value = (samples, 24000)
    mock_module.Kokoro.return_value = mock_model
    return mock_module, mock_model


@pytest.fixture
def kokoro_engine(mock_kokoro_module):
    """Create a KokoroEngine with mocked kokoro_onnx."""
    mock_module, mock_model = mock_kokoro_module
    with patch.dict("sys.modules", {"kokoro_onnx": mock_module}):
        from tts.kokoro_engine import KokoroEngine
        engine = KokoroEngine("fake_model.onnx", "fake_voices.bin")
    return engine, mock_model


@pytest.fixture
def audio_player():
    """Create an AudioPlayer with command existence mocked."""
    with patch("tts.audio_player.AudioPlayer._command_exists", return_value=True):
        from tts.audio_player import AudioPlayer
        player = AudioPlayer("mpv")
    return player


@pytest.fixture
def mock_popen():
    """Provide a mock subprocess.Popen that succeeds."""
    with patch("subprocess.Popen") as mock_cls:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_proc.poll.return_value = None
        mock_cls.return_value = mock_proc
        yield mock_cls, mock_proc


@pytest.fixture
def speech_queue():
    """Create a SpeechQueue with mocked engine and player."""
    mock_engine = MagicMock()
    mock_player = MagicMock()

    samples = np.zeros(24000, dtype=np.float32)
    mock_engine.synthesize.return_value = SynthesisResult(
        samples=samples,
        sample_rate=24000,
        duration_ms=1000.0,
        synthesis_ms=100.0,
    )
    mock_player.play.return_value = PlaybackResult(
        success=True,
        duration_ms=1000.0,
    )

    from tts.speech_queue import SpeechQueue
    queue = SpeechQueue(mock_engine, mock_player)
    return queue, mock_engine, mock_player


@pytest.fixture
def audio_samples():
    """Provide a 1-second numpy audio sample array."""
    return np.zeros(24000, dtype=np.float32)


# ─── KokoroEngine Tests ─────────────────────────────────────────────────────


class TestKokoroEngine:
    """Tests for KokoroEngine."""

    def test_synthesize_returns_correct_format(self, kokoro_engine):
        engine, mock_model = kokoro_engine
        result = engine.synthesize("Hello world", "am_eric", speed=1.0)

        assert isinstance(result, SynthesisResult)
        assert result.sample_rate == 24000
        assert result.duration_ms > 0
        assert result.synthesis_ms >= 0
        assert result.samples is not None
        mock_model.create.assert_called_once_with(
            "Hello world", voice="am_eric", speed=1.0
        )

    def test_synthesize_validates_voice_id(self, kokoro_engine):
        engine, _ = kokoro_engine
        with pytest.raises(TTSEngineError, match="Invalid voice_id"):
            engine.synthesize("Hello", "invalid_voice")

    def test_synthesize_rejects_empty_voice_id(self, kokoro_engine):
        engine, _ = kokoro_engine
        with pytest.raises(TTSEngineError, match="Invalid voice_id"):
            engine.synthesize("Hello", "")

    def test_synthesize_accepts_all_valid_voices(self, kokoro_engine):
        engine, _ = kokoro_engine
        # Spot-check a sample of voice IDs from different categories
        for voice in ["af_nova", "am_fenrir", "bf_alice", "bm_daniel", "jf_alpha", "zm_yunxi"]:
            result = engine.synthesize("Hello", voice)
            assert result.sample_rate == 24000

    def test_is_loaded(self, kokoro_engine):
        engine, _ = kokoro_engine
        assert engine.is_loaded() is True

    def test_init_failure_raises_tts_engine_error(self):
        mock_kokoro = MagicMock()
        mock_kokoro.Kokoro.side_effect = RuntimeError("Model file not found")

        with patch.dict("sys.modules", {"kokoro_onnx": mock_kokoro}):
            from tts.kokoro_engine import KokoroEngine
            with pytest.raises(TTSEngineError, match="Failed to load"):
                KokoroEngine("bad_path.onnx", "bad_voices.bin")

    def test_synthesize_when_not_loaded(self, kokoro_engine):
        engine, _ = kokoro_engine
        engine._loaded = False

        with pytest.raises(TTSEngineError, match="not loaded"):
            engine.synthesize("Hello", "am_eric")

    def test_synthesize_with_default_speed(self, kokoro_engine):
        engine, mock_model = kokoro_engine
        engine.synthesize("Normal speed", "am_eric")
        mock_model.create.assert_called_once_with(
            "Normal speed", voice="am_eric", speed=1.0
        )

    def test_synthesize_with_fast_speed(self, kokoro_engine):
        engine, mock_model = kokoro_engine
        engine.synthesize("Fast speech", "am_eric", speed=1.5)
        mock_model.create.assert_called_once_with(
            "Fast speech", voice="am_eric", speed=1.5
        )

    def test_synthesize_with_slow_speed(self, kokoro_engine):
        engine, mock_model = kokoro_engine
        engine.synthesize("Slow speech", "am_eric", speed=0.5)
        mock_model.create.assert_called_once_with(
            "Slow speech", voice="am_eric", speed=0.5
        )

    def test_synthesize_duration_matches_samples(self, mock_kokoro_module):
        """Verify duration_ms is computed from sample count / sample_rate."""
        mock_module, mock_model = mock_kokoro_module
        # 48000 samples at 24000 Hz = 2 seconds = 2000 ms
        samples = np.zeros(48000, dtype=np.float32)
        mock_model.create.return_value = (samples, 24000)

        with patch.dict("sys.modules", {"kokoro_onnx": mock_module}):
            from tts.kokoro_engine import KokoroEngine
            engine = KokoroEngine("fake.onnx", "fake.bin")

        result = engine.synthesize("Long text", "am_eric")
        assert abs(result.duration_ms - 2000.0) < 1.0

    def test_synthesize_propagates_engine_error(self, kokoro_engine):
        engine, mock_model = kokoro_engine
        mock_model.create.side_effect = RuntimeError("ONNX runtime error")

        with pytest.raises(TTSEngineError, match="Synthesis failed"):
            engine.synthesize("Hello", "am_eric")


# ─── AudioPlayer Tests ──────────────────────────────────────────────────────


class TestAudioPlayer:
    """Tests for AudioPlayer."""

    def test_play_writes_temp_file_and_cleans_up(self, audio_player, audio_samples, mock_popen):
        mock_cls, mock_proc = mock_popen
        result = audio_player.play(audio_samples, 24000)

        assert isinstance(result, PlaybackResult)
        assert result.success is True
        assert result.duration_ms >= 0

        # Verify Popen was called with mpv command
        call_args = mock_cls.call_args[0][0]
        assert call_args[0] == "mpv"
        assert call_args[1] == "--no-terminal"
        assert call_args[2] == "--no-video"
        assert call_args[3].endswith(".wav")
        # Temp file should have been cleaned up
        assert not Path(call_args[3]).exists()

    def test_play_reports_failure_on_nonzero_exit(self, audio_player, audio_samples, mock_popen):
        mock_cls, mock_proc = mock_popen
        mock_proc.returncode = 1

        result = audio_player.play(audio_samples, 24000)

        assert result.success is False
        assert "exited with code 1" in result.error

    def test_play_cleans_up_temp_file_on_failure(self, audio_player, audio_samples, mock_popen):
        mock_cls, mock_proc = mock_popen
        mock_proc.returncode = 1

        audio_player.play(audio_samples, 24000)

        # Temp file should still be cleaned up even on failure
        call_args = mock_cls.call_args[0][0]
        assert not Path(call_args[3]).exists()

    def test_stop_kills_process(self, audio_player):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        audio_player._process = mock_proc

        result = audio_player.stop()

        assert result is True
        mock_proc.kill.assert_called_once()

    def test_stop_returns_false_when_nothing_playing(self, audio_player):
        result = audio_player.stop()
        assert result is False

    def test_stop_clears_process_reference(self, audio_player):
        mock_proc = MagicMock()
        audio_player._process = mock_proc

        audio_player.stop()

        assert audio_player._process is None

    def test_is_playing_false_initially(self, audio_player):
        assert audio_player.is_playing is False

    def test_is_playing_true_when_running(self, audio_player):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        audio_player._process = mock_proc
        assert audio_player.is_playing is True

    def test_is_playing_false_when_finished(self, audio_player):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # finished
        audio_player._process = mock_proc
        assert audio_player.is_playing is False

    def test_is_playing_lifecycle(self, audio_player):
        """Test the full is_playing lifecycle: not playing -> playing -> finished."""
        assert audio_player.is_playing is False

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        audio_player._process = mock_proc
        assert audio_player.is_playing is True

        mock_proc.poll.return_value = 0
        assert audio_player.is_playing is False

    def test_fallback_to_afplay_on_macos(self):
        with patch("tts.audio_player.AudioPlayer._command_exists", side_effect=lambda cmd: cmd != "mpv"):
            with patch("platform.system", return_value="Darwin"):
                from tts.audio_player import AudioPlayer
                player = AudioPlayer("mpv")
                assert player._player_command == "afplay"

    def test_fallback_to_aplay_on_linux(self):
        with patch("tts.audio_player.AudioPlayer._command_exists", side_effect=lambda cmd: cmd != "mpv"):
            with patch("platform.system", return_value="Linux"):
                from tts.audio_player import AudioPlayer
                player = AudioPlayer("mpv")
                assert player._player_command == "aplay"

    def test_build_command_mpv(self, audio_player):
        cmd = audio_player._build_command("/tmp/test.wav")
        assert cmd == ["mpv", "--no-terminal", "--no-video", "/tmp/test.wav"]

    def test_build_command_afplay(self):
        with patch("tts.audio_player.AudioPlayer._command_exists", return_value=True):
            from tts.audio_player import AudioPlayer
            player = AudioPlayer("afplay")
        cmd = player._build_command("/tmp/test.wav")
        assert cmd == ["afplay", "/tmp/test.wav"]

    def test_process_cleared_after_play(self, audio_player, audio_samples, mock_popen):
        """After play() completes, _process should be None."""
        audio_player.play(audio_samples, 24000)
        assert audio_player._process is None


# ─── SpeechQueue chunk_text Tests ────────────────────────────────────────────


class TestSpeechQueueChunking:
    """Tests for SpeechQueue.chunk_text static method."""

    def test_empty_text(self):
        from tts.speech_queue import SpeechQueue
        assert SpeechQueue.chunk_text("") == []

    def test_short_text_single_chunk(self):
        from tts.speech_queue import SpeechQueue
        result = SpeechQueue.chunk_text("Hello world.", max_length=500)
        assert result == ["Hello world."]

    def test_text_under_max_length(self):
        from tts.speech_queue import SpeechQueue
        text = "Short sentence."
        result = SpeechQueue.chunk_text(text, max_length=500)
        assert result == [text]

    def test_splits_on_period(self):
        from tts.speech_queue import SpeechQueue
        text = "First sentence. Second sentence. Third sentence."
        result = SpeechQueue.chunk_text(text, max_length=35)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) > 0

    def test_splits_on_exclamation(self):
        from tts.speech_queue import SpeechQueue
        text = "Wow! Amazing! Incredible!"
        result = SpeechQueue.chunk_text(text, max_length=15)
        assert len(result) >= 2

    def test_splits_on_question_mark(self):
        from tts.speech_queue import SpeechQueue
        text = "What happened? Where are you? Are you okay?"
        result = SpeechQueue.chunk_text(text, max_length=20)
        assert len(result) >= 2

    def test_mixed_punctuation(self):
        from tts.speech_queue import SpeechQueue
        text = "Wow! What happened? Let me check. All good."
        result = SpeechQueue.chunk_text(text, max_length=25)
        assert len(result) >= 2

    def test_long_single_sentence_not_broken(self):
        from tts.speech_queue import SpeechQueue
        long_sentence = "A" * 600
        result = SpeechQueue.chunk_text(long_sentence, max_length=500)
        assert result == [long_sentence]

    def test_multiple_sentences_grouped_under_limit(self):
        from tts.speech_queue import SpeechQueue
        text = "Short. Also short. And short."
        result = SpeechQueue.chunk_text(text, max_length=500)
        assert result == [text]

    def test_default_max_length_is_500(self):
        from tts.speech_queue import SpeechQueue
        text = "A" * 400
        result = SpeechQueue.chunk_text(text)
        assert result == [text]

    def test_exactly_at_max_length(self):
        from tts.speech_queue import SpeechQueue
        text = "A" * 500
        result = SpeechQueue.chunk_text(text, max_length=500)
        assert result == [text]

    def test_reconstructed_text_preserves_content(self):
        """All original sentences should appear in the chunked output."""
        from tts.speech_queue import SpeechQueue
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = SpeechQueue.chunk_text(text, max_length=40)
        joined = " ".join(result)
        assert "First sentence." in joined
        assert "Second sentence." in joined
        assert "Third sentence." in joined
        assert "Fourth sentence." in joined


# ─── SpeechQueue speak Tests ────────────────────────────────────────────────


class TestSpeechQueueSpeak:
    """Tests for SpeechQueue.speak (async)."""

    @pytest.mark.asyncio
    async def test_speak_blocking_returns_speak_result(self, speech_queue):
        queue, mock_engine, mock_player = speech_queue

        result = await queue.speak("Hello world", "am_eric", block=True)

        assert isinstance(result, SpeakResult)
        assert result.success is True
        assert result.voice == "am_eric"
        assert result.duration_ms > 0
        assert result.synthesis_ms > 0
        assert result.queued is False

    @pytest.mark.asyncio
    async def test_speak_nonblocking_returns_queued(self, speech_queue):
        queue, _, _ = speech_queue

        result = await queue.speak("Hello world", "am_eric", block=False)

        assert isinstance(result, SpeakResult)
        assert result.success is True
        assert result.queued is True
        assert result.voice == "am_eric"

        # Give the background task a moment to run
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_speak_nonblocking_does_not_wait(self, speech_queue):
        """Non-blocking speak should return nearly instantly."""
        queue, _, _ = speech_queue

        import time
        start = time.perf_counter()
        result = await queue.speak("Hello world", "am_eric", block=False)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert result.queued is True
        # Should return in well under 100ms (no synthesis/playback wait)
        assert elapsed_ms < 100

        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_speak_blocking_handles_playback_failure(self, speech_queue):
        queue, mock_engine, mock_player = speech_queue
        mock_player.play.return_value = PlaybackResult(
            success=False,
            duration_ms=0.0,
            error="Player crashed",
        )

        result = await queue.speak("Hello", "am_eric", block=True)

        assert result.success is False
        assert "Player crashed" in result.error

    @pytest.mark.asyncio
    async def test_speak_blocking_calls_engine_and_player(self, speech_queue):
        queue, mock_engine, mock_player = speech_queue

        await queue.speak("Test text", "am_eric", speed=1.2, block=True)

        mock_engine.synthesize.assert_called_once_with("Test text", "am_eric", 1.2)
        mock_player.play.assert_called_once()

    @pytest.mark.asyncio
    async def test_speak_blocking_passes_speed_to_engine(self, speech_queue):
        queue, mock_engine, _ = speech_queue

        await queue.speak("Fast", "am_eric", speed=2.0, block=True)
        mock_engine.synthesize.assert_called_with("Fast", "am_eric", 2.0)

    @pytest.mark.asyncio
    async def test_speak_blocking_auto_chunks_long_text(self, speech_queue):
        """Long text should be auto-chunked and each chunk synthesized separately."""
        queue, mock_engine, mock_player = speech_queue

        # Build text with multiple sentences exceeding 500 chars total
        sentences = [f"This is sentence number {i}." for i in range(30)]
        long_text = " ".join(sentences)
        assert len(long_text) > 500

        result = await queue.speak(long_text, "am_eric", block=True)

        assert result.success is True
        # Engine should be called multiple times (once per chunk)
        assert mock_engine.synthesize.call_count > 1
        # Player should be called the same number of times
        assert mock_player.play.call_count == mock_engine.synthesize.call_count

    @pytest.mark.asyncio
    async def test_speak_blocking_accumulates_timing(self, speech_queue):
        """Total timing should sum across chunks."""
        queue, mock_engine, mock_player = speech_queue

        # Two sentences that will fit in one chunk
        result = await queue.speak("Hello. World.", "am_eric", block=True)

        assert result.success is True
        assert result.synthesis_ms >= 100.0  # At least one chunk's worth
        assert result.duration_ms >= 1000.0

    @pytest.mark.asyncio
    async def test_speak_handles_synthesis_exception(self, speech_queue):
        queue, mock_engine, _ = speech_queue
        mock_engine.synthesize.side_effect = TTSEngineError("Engine failed")

        result = await queue.speak("Hello", "am_eric", block=True)

        assert result.success is False
        assert "Engine failed" in result.error

    def test_stop_delegates_to_player(self, speech_queue):
        queue, _, mock_player = speech_queue
        mock_player.stop.return_value = True

        result = queue.stop()

        assert result is True
        mock_player.stop.assert_called_once()

    def test_stop_returns_false_when_nothing_playing(self, speech_queue):
        queue, _, mock_player = speech_queue
        mock_player.stop.return_value = False

        result = queue.stop()

        assert result is False

    def test_depth_returns_zero_initially(self, speech_queue):
        queue, _, _ = speech_queue
        assert queue.depth == 0
