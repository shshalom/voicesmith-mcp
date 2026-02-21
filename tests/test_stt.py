"""Tests for STT subsystem (whisper_engine, vad, mic_capture)."""

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import TranscriptionResult, STTEngineError, VADError, MicCaptureError


# ─── WhisperEngine Tests ─────────────────────────────────────────────────────


class TestWhisperEngine:
    """Tests for WhisperEngine transcription."""

    def _make_engine(self):
        """Create a WhisperEngine with a mocked faster_whisper module."""
        mock_fw = MagicMock()
        mock_model = MagicMock()
        mock_fw.WhisperModel.return_value = mock_model

        with patch.dict("sys.modules", {"faster_whisper": mock_fw}):
            from stt.whisper_engine import WhisperEngine
            engine = WhisperEngine(model_size="base", language="en")

        return engine, mock_model

    def test_engine_loads_successfully(self):
        engine, _ = self._make_engine()
        assert engine.is_loaded() is True

    def test_engine_load_failure(self):
        mock_fw = MagicMock()
        mock_fw.WhisperModel.side_effect = RuntimeError("model not found")

        with patch.dict("sys.modules", {"faster_whisper": mock_fw}):
            from stt.whisper_engine import WhisperEngine
            with pytest.raises(STTEngineError, match="Failed to load Whisper model"):
                WhisperEngine(model_size="base", language="en")

    def test_transcribe_returns_correct_format(self):
        engine, mock_model = self._make_engine()

        seg1 = MagicMock()
        seg1.text = "Hello world"
        seg1.avg_logprob = -0.3

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = ([seg1], mock_info)

        audio = np.zeros(16000, dtype=np.float32)
        result = engine.transcribe(audio)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hello world"
        assert result.language == "en"
        assert result.transcription_ms > 0

    def test_transcribe_confidence_computation(self):
        """Confidence should be exp(avg_logprob) averaged across segments."""
        engine, mock_model = self._make_engine()

        seg1 = MagicMock()
        seg1.text = "Hello "
        seg1.avg_logprob = -0.2

        seg2 = MagicMock()
        seg2.text = "world"
        seg2.avg_logprob = -0.4

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = ([seg1, seg2], mock_info)

        audio = np.zeros(16000, dtype=np.float32)
        result = engine.transcribe(audio)

        expected_avg = (-0.2 + -0.4) / 2  # -0.3
        expected_confidence = math.exp(expected_avg)
        assert abs(result.confidence - expected_confidence) < 1e-6

    def test_transcribe_no_segments(self):
        """Empty segments should return empty text and 0.0 confidence."""
        engine, mock_model = self._make_engine()

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = ([], mock_info)

        audio = np.zeros(16000, dtype=np.float32)
        result = engine.transcribe(audio)

        assert result.text == ""
        assert result.confidence == 0.0

    def test_transcribe_not_loaded(self):
        engine, _ = self._make_engine()
        engine._loaded = False

        audio = np.zeros(16000, dtype=np.float32)
        with pytest.raises(STTEngineError, match="not loaded"):
            engine.transcribe(audio)

    def test_transcribe_joins_multiple_segments(self):
        engine, mock_model = self._make_engine()

        seg1 = MagicMock()
        seg1.text = " Hello "
        seg1.avg_logprob = -0.1

        seg2 = MagicMock()
        seg2.text = "world "
        seg2.avg_logprob = -0.1

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = ([seg1, seg2], mock_info)

        audio = np.zeros(16000, dtype=np.float32)
        result = engine.transcribe(audio)

        assert result.text == "Hello world"

    def test_transcribe_confidence_clamped(self):
        """Confidence should be clamped to [0.0, 1.0]."""
        engine, mock_model = self._make_engine()

        seg1 = MagicMock()
        seg1.text = "Hello"
        seg1.avg_logprob = 0.5  # exp(0.5) > 1.0

        mock_info = MagicMock()
        mock_info.language = "en"
        mock_model.transcribe.return_value = ([seg1], mock_info)

        audio = np.zeros(16000, dtype=np.float32)
        result = engine.transcribe(audio)

        assert result.confidence == 1.0


# ─── VoiceActivityDetector Tests ─────────────────────────────────────────────


class TestVoiceActivityDetector:
    """Tests for VoiceActivityDetector (ONNX Runtime)."""

    def _make_vad(self, speech_prob=0.8):
        """Create a VoiceActivityDetector with mocked onnxruntime."""
        mock_ort = MagicMock()
        mock_session = MagicMock()
        # session.run returns [output, new_state]
        mock_session.run.return_value = [
            np.array([[speech_prob]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        ]
        mock_ort.InferenceSession.return_value = mock_session

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            with patch("stt.vad.VoiceActivityDetector._find_model", return_value="/fake/silero_vad.onnx"):
                from stt.vad import VoiceActivityDetector
                vad = VoiceActivityDetector()

        return vad, mock_session

    def test_vad_loads_successfully(self):
        vad, _ = self._make_vad()
        assert vad.is_loaded() is True

    def test_vad_load_failure(self):
        mock_ort = MagicMock()
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            with patch("stt.vad.VoiceActivityDetector._find_model", return_value=None):
                from stt.vad import VoiceActivityDetector
                with pytest.raises(VADError, match="not found"):
                    VoiceActivityDetector()

    def test_is_speech_returns_bool_true(self):
        vad, _ = self._make_vad(speech_prob=0.8)
        chunk = np.zeros(512, dtype=np.float32)
        result = vad.is_speech(chunk)
        assert isinstance(result, bool)
        assert result is True

    def test_is_speech_returns_bool_false(self):
        vad, _ = self._make_vad(speech_prob=0.1)
        chunk = np.zeros(512, dtype=np.float32)
        result = vad.is_speech(chunk)
        assert result is False

    def test_speech_probability_returns_float(self):
        vad, _ = self._make_vad(speech_prob=0.65)
        chunk = np.zeros(512, dtype=np.float32)
        prob = vad.speech_probability(chunk)
        assert isinstance(prob, float)
        assert 0.0 <= prob <= 1.0
        assert abs(prob - 0.65) < 0.01

    def test_speech_probability_with_bytes(self):
        vad, _ = self._make_vad(speech_prob=0.7)
        chunk_array = np.zeros(512, dtype=np.float32)
        chunk_bytes = chunk_array.tobytes()
        prob = vad.speech_probability(chunk_bytes)
        assert isinstance(prob, float)

    def test_reset(self):
        vad, _ = self._make_vad()
        vad.reset()
        # After reset, state should be zeros
        assert np.allclose(vad._state, np.zeros((2, 1, 128), dtype=np.float32))

    def test_not_loaded_raises(self):
        vad, _ = self._make_vad()
        vad._loaded = False
        chunk = np.zeros(512, dtype=np.float32)
        with pytest.raises(VADError, match="not loaded"):
            vad.speech_probability(chunk)


# ─── MicCapture Tests ────────────────────────────────────────────────────────


class TestMicCapture:
    """Tests for MicCapture recording."""

    def test_initial_state(self):
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        assert mic.is_recording is False

    def _mock_sounddevice(self):
        """Create a mock sounddevice module and inject it into sys.modules."""
        mock_sd = MagicMock()
        mock_stream = MagicMock()
        mock_sd.InputStream.return_value = mock_stream
        return mock_sd, mock_stream

    @pytest.mark.asyncio
    async def test_record_timeout_returns_none(self):
        """Recording with no speech should return None after timeout."""
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        mock_vad = MagicMock()
        mock_vad.is_speech.return_value = False

        mock_sd, _ = self._mock_sounddevice()

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            # Put some silent chunks in the queue, then let it timeout
            async def feed_silence():
                await asyncio.sleep(0.05)
                for _ in range(5):
                    mic._audio_queue.put(np.zeros((512, 1), dtype=np.float32))

            task = asyncio.create_task(feed_silence())
            result = await mic.record(vad=mock_vad, timeout=0.5, silence_threshold=0.3)
            await task

        assert result is None
        assert mic.is_recording is False

    @pytest.mark.asyncio
    async def test_record_with_cancellation(self):
        """Recording should return None when cancel_event is set."""
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        mock_vad = MagicMock()
        mock_vad.is_speech.return_value = False

        cancel_event = asyncio.Event()
        mock_sd, _ = self._mock_sounddevice()

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            async def cancel_soon():
                await asyncio.sleep(0.1)
                cancel_event.set()

            task = asyncio.create_task(cancel_soon())
            result = await mic.record(
                vad=mock_vad, timeout=10, cancel_event=cancel_event
            )
            await task

        assert result is None
        assert mic.is_recording is False

    @pytest.mark.asyncio
    async def test_record_concurrent_call_protection(self):
        """Second record call should raise MicCaptureError if already recording."""
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        mic._recording = True

        mock_vad = MagicMock()
        with pytest.raises(MicCaptureError, match="Another recording"):
            await mic.record(vad=mock_vad)

    @pytest.mark.asyncio
    async def test_record_with_speech_and_silence(self):
        """Recording should capture speech and stop after silence threshold."""
        from stt.mic_capture import MicCapture

        mic = MicCapture(sample_rate=16000)

        # VAD returns True for first 3 chunks (speech), then False (silence)
        call_count = 0

        def mock_is_speech(chunk):
            nonlocal call_count
            call_count += 1
            return call_count <= 3

        mock_vad = MagicMock()
        mock_vad.is_speech.side_effect = mock_is_speech

        mock_sd, _ = self._mock_sounddevice()

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            # Each chunk is 1600 samples at 16kHz = 0.1s
            async def feed_audio():
                await asyncio.sleep(0.05)
                for _ in range(20):
                    mic._audio_queue.put(
                        np.random.randn(1600, 1).astype(np.float32)
                    )
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(feed_audio())
            result = await mic.record(
                vad=mock_vad, timeout=5, silence_threshold=0.5
            )
            await task

        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.ndim == 1
        assert mic.is_recording is False

    def test_stop_sets_flag(self):
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        mic.stop()
        assert mic._stop_flag is True

    def test_audio_callback(self):
        from stt.mic_capture import MicCapture

        mic = MicCapture()
        indata = np.ones((512, 1), dtype=np.float32)
        mic._audio_callback(indata, 512, None, None)

        assert not mic._audio_queue.empty()
        queued = mic._audio_queue.get()
        np.testing.assert_array_equal(queued, indata)
