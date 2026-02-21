"""Kokoro ONNX TTS engine wrapper."""

import time

from shared import SynthesisResult, TTSEngineError, ALL_VOICE_IDS, SAMPLE_RATE, get_logger

logger = get_logger("tts.kokoro")


class KokoroEngine:
    """Wrapper around kokoro-onnx for text-to-speech synthesis."""

    def __init__(self, model_path: str, voices_path: str) -> None:
        self._loaded = False
        try:
            import kokoro_onnx
            self._model = kokoro_onnx.Kokoro(model_path, voices_path)
            self._loaded = True
            logger.info("Kokoro TTS engine loaded successfully")
        except Exception as e:
            raise TTSEngineError(f"Failed to load Kokoro model: {e}") from e

    def synthesize(self, text: str, voice_id: str, speed: float = 1.0) -> SynthesisResult:
        """Synthesize text to audio samples.

        Args:
            text: The text to synthesize.
            voice_id: A valid Kokoro voice ID (e.g. "am_eric").
            speed: Speech speed multiplier (default 1.0).

        Returns:
            SynthesisResult with samples, sample_rate, duration_ms, synthesis_ms.

        Raises:
            TTSEngineError: If voice_id is invalid or synthesis fails.
        """
        if voice_id not in ALL_VOICE_IDS:
            raise TTSEngineError(
                f"Invalid voice_id '{voice_id}'. Use a valid voice from ALL_VOICE_IDS."
            )

        if not self._loaded:
            raise TTSEngineError("Kokoro engine is not loaded")

        try:
            start = time.perf_counter()
            samples, sample_rate = self._model.create(text, voice=voice_id, speed=speed)
            synthesis_ms = (time.perf_counter() - start) * 1000

            duration_ms = (len(samples) / sample_rate) * 1000

            return SynthesisResult(
                samples=samples,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                synthesis_ms=synthesis_ms,
            )
        except Exception as e:
            raise TTSEngineError(f"Synthesis failed: {e}") from e

    def is_loaded(self) -> bool:
        """Return whether the engine is loaded and ready."""
        return self._loaded
