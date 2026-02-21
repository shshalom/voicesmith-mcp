"""Silero VAD integration for voice activity detection (ONNX Runtime, no torch)."""

import os

import numpy as np

from shared import VADError, STT_SAMPLE_RATE, get_logger

logger = get_logger("stt.vad")


class VoiceActivityDetector:
    """Voice Activity Detection using Silero VAD via ONNX Runtime.

    Uses the ONNX model directly — no PyTorch dependency.
    The model is stateful (LSTM hidden state persists between chunks).
    """

    def __init__(self) -> None:
        self._loaded = False
        self._session = None
        self._state = None
        self._sr = None

        try:
            import onnxruntime as ort

            # Find the Silero VAD ONNX model
            model_path = self._find_model()
            if model_path is None:
                raise VADError("silero_vad.onnx not found. Install with: pip install silero-vad")

            self._session = ort.InferenceSession(model_path)
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._sr = np.array(STT_SAMPLE_RATE, dtype=np.int64)
            self._loaded = True
            logger.info(f"Silero VAD loaded (ONNX) from {model_path}")
        except VADError:
            raise
        except Exception as e:
            raise VADError(f"Failed to load Silero VAD: {e}") from e

    @staticmethod
    def _find_model() -> str | None:
        """Locate the silero_vad.onnx model file."""
        # Check silero-vad pip package location
        try:
            import silero_vad
            pkg_path = os.path.join(os.path.dirname(silero_vad.__file__), "data", "silero_vad.onnx")
            if os.path.exists(pkg_path):
                return pkg_path
        except ImportError:
            pass

        # Check common locations
        for path in [
            os.path.expanduser("~/.local/share/agent-voice-mcp/models/silero_vad.onnx"),
            os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx"),
        ]:
            if os.path.exists(path):
                return path

        return None

    def is_speech(self, chunk: bytes | np.ndarray) -> bool:
        """Return True if speech is detected in the audio chunk.

        Args:
            chunk: Audio data as bytes or numpy ndarray (float32, 16kHz mono).

        Returns:
            True if speech probability exceeds 0.5.
        """
        return self.speech_probability(chunk) > 0.5

    def speech_probability(self, chunk: bytes | np.ndarray) -> float:
        """Return speech probability for the audio chunk.

        Args:
            chunk: Audio data as bytes or numpy ndarray (float32, 16kHz mono).
                   Expected chunk size: 512 samples (32ms at 16kHz).

        Returns:
            Float between 0.0 and 1.0 indicating speech probability.
        """
        if not self._loaded:
            raise VADError("VAD model is not loaded")

        try:
            if isinstance(chunk, bytes):
                audio = np.frombuffer(chunk, dtype=np.float32)
            else:
                audio = chunk.astype(np.float32) if chunk.dtype != np.float32 else chunk

            # Flatten to 1D if needed
            if audio.ndim > 1:
                audio = audio.flatten()

            # Reshape to [1, chunk_size] for ONNX model
            audio = audio.reshape(1, -1)

            output, new_state = self._session.run(
                None,
                {"input": audio, "state": self._state, "sr": self._sr},
            )

            self._state = new_state
            prob = float(output[0][0])
            return max(0.0, min(1.0, prob))
        except VADError:
            raise
        except Exception as e:
            raise VADError(f"VAD inference failed: {e}") from e

    def reset(self) -> None:
        """Reset VAD state between recordings."""
        if self._loaded:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def is_loaded(self) -> bool:
        """Return whether the VAD model is loaded and ready."""
        return self._loaded
