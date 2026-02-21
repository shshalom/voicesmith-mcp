"""faster-whisper STT engine wrapper."""

import math
import time

import numpy as np

from shared import TranscriptionResult, STTEngineError, get_logger

logger = get_logger("stt.whisper")


class WhisperEngine:
    """Wrapper around faster-whisper for speech-to-text transcription."""

    def __init__(self, model_size: str = "base", language: str = "en") -> None:
        self._loaded = False
        self._language = language
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(model_size, device="auto", compute_type="auto")
            self._loaded = True
            logger.info(f"Whisper STT engine loaded (model={model_size}, language={language})")
        except Exception as e:
            raise STTEngineError(f"Failed to load Whisper model: {e}") from e

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        """Transcribe audio to text.

        Args:
            audio: Audio samples as numpy ndarray (float32).
            sample_rate: Sample rate of the audio (default 16000).

        Returns:
            TranscriptionResult with text, confidence, transcription_ms, language.

        Raises:
            STTEngineError: If transcription fails.
        """
        if not self._loaded:
            raise STTEngineError("Whisper engine is not loaded")

        try:
            start = time.perf_counter()
            segments, info = self._model.transcribe(audio, language=self._language)

            # Collect all segments
            texts = []
            log_probs = []
            for segment in segments:
                texts.append(segment.text)
                log_probs.append(segment.avg_logprob)

            transcription_ms = (time.perf_counter() - start) * 1000
            text = "".join(texts).strip()

            # Compute confidence as exp(average log probability)
            if log_probs:
                avg_log_prob = sum(log_probs) / len(log_probs)
                confidence = math.exp(avg_log_prob)
            else:
                confidence = 0.0

            # Clamp confidence to [0.0, 1.0]
            confidence = max(0.0, min(1.0, confidence))

            return TranscriptionResult(
                text=text,
                confidence=confidence,
                transcription_ms=transcription_ms,
                language=info.language if hasattr(info, "language") else self._language,
            )
        except STTEngineError:
            raise
        except Exception as e:
            raise STTEngineError(f"Transcription failed: {e}") from e

    def is_loaded(self) -> bool:
        """Return whether the engine is loaded and ready."""
        return self._loaded
