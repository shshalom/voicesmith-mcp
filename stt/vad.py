"""Silero VAD integration for voice activity detection."""

import numpy as np

from shared import VADError, STT_SAMPLE_RATE, get_logger

logger = get_logger("stt.vad")


class VoiceActivityDetector:
    """Voice Activity Detection using Silero VAD."""

    def __init__(self) -> None:
        self._loaded = False
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
            )
            self._model = model
            self._loaded = True
            logger.info("Silero VAD loaded successfully")
        except Exception as e:
            raise VADError(f"Failed to load Silero VAD: {e}") from e

    def is_speech(self, chunk: bytes | np.ndarray) -> bool:
        """Return True if speech is detected in the audio chunk.

        Args:
            chunk: Audio data as bytes or numpy ndarray (float32, 16kHz mono).

        Returns:
            True if speech probability exceeds 0.5.

        Raises:
            VADError: If VAD inference fails.
        """
        return self.speech_probability(chunk) > 0.5

    def speech_probability(self, chunk: bytes | np.ndarray) -> float:
        """Return speech probability for the audio chunk.

        Args:
            chunk: Audio data as bytes or numpy ndarray (float32, 16kHz mono).

        Returns:
            Float between 0.0 and 1.0 indicating speech probability.

        Raises:
            VADError: If VAD inference fails.
        """
        if not self._loaded:
            raise VADError("VAD model is not loaded")

        try:
            import torch

            if isinstance(chunk, bytes):
                audio = np.frombuffer(chunk, dtype=np.float32)
            else:
                audio = chunk.astype(np.float32) if chunk.dtype != np.float32 else chunk

            # Flatten to 1D if needed
            if audio.ndim > 1:
                audio = audio.flatten()

            tensor = torch.from_numpy(audio)
            prob = self._model(tensor, STT_SAMPLE_RATE).item()
            return float(prob)
        except VADError:
            raise
        except Exception as e:
            raise VADError(f"VAD inference failed: {e}") from e

    def reset(self) -> None:
        """Reset VAD state between recordings."""
        if self._loaded:
            self._model.reset_states()

    def is_loaded(self) -> bool:
        """Return whether the VAD model is loaded and ready."""
        return self._loaded
