"""Microphone capture with VAD-controlled recording."""

import asyncio
import queue
from typing import Optional

import numpy as np

from shared import MicCaptureError, STT_SAMPLE_RATE, get_logger
from stt.vad import VoiceActivityDetector

logger = get_logger("stt.mic")


class MicCapture:
    """Microphone capture with voice activity detection."""

    def __init__(self, sample_rate: int = STT_SAMPLE_RATE) -> None:
        self._sample_rate = sample_rate
        self._recording = False
        self._audio_queue: queue.Queue = queue.Queue()
        self._stop_flag = False

    async def record(
        self,
        vad: VoiceActivityDetector,
        timeout: float = 15,
        silence_threshold: float = 1.5,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[np.ndarray]:
        """Record audio from the microphone until silence is detected.

        Uses VAD to detect speech and stop recording after a period of silence.

        Args:
            vad: VoiceActivityDetector instance for speech detection.
            timeout: Maximum seconds to wait for speech (default 15).
            silence_threshold: Seconds of silence before stopping (default 1.5).
            cancel_event: Optional asyncio.Event to cancel recording.

        Returns:
            Numpy array of recorded audio, or None if cancelled/timeout.

        Raises:
            MicCaptureError: If microphone access fails.
        """
        if self._recording:
            raise MicCaptureError("Another recording is already in progress")

        try:
            import sounddevice as sd
        except Exception as e:
            raise MicCaptureError(f"Failed to import sounddevice: {e}") from e

        self._recording = True
        self._stop_flag = False
        self._audio_queue = queue.Queue()
        chunks: list[np.ndarray] = []
        speech_detected = False
        silence_duration = 0.0
        loop = asyncio.get_event_loop()

        try:
            stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                blocksize=512,  # Silero VAD expects 512-sample chunks at 16kHz
                callback=self._audio_callback,
            )
            stream.start()
            logger.info("Microphone recording started")

            start_time = asyncio.get_event_loop().time()

            while not self._stop_flag:
                # Check cancellation
                if cancel_event and cancel_event.is_set():
                    logger.info("Recording cancelled by event")
                    return None

                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    if not speech_detected:
                        logger.info("Recording timed out with no speech detected")
                        return None
                    logger.info("Recording timed out")
                    break

                # Get audio chunk from queue
                try:
                    chunk = await loop.run_in_executor(
                        None, self._audio_queue.get, True, 0.1
                    )
                except queue.Empty:
                    continue

                chunks.append(chunk)
                is_speech = vad.is_speech(chunk)

                if is_speech:
                    speech_detected = True
                    silence_duration = 0.0
                elif speech_detected:
                    # Count silence after speech was detected
                    chunk_duration = len(chunk) / self._sample_rate
                    silence_duration += chunk_duration
                    if silence_duration >= silence_threshold:
                        logger.info(
                            f"Silence threshold reached ({silence_threshold}s), stopping"
                        )
                        break

            stream.stop()
            stream.close()

            if not chunks or not speech_detected:
                return None

            return np.concatenate(chunks).flatten()

        except MicCaptureError:
            raise
        except Exception as e:
            raise MicCaptureError(f"Recording failed: {e}") from e
        finally:
            self._recording = False

    def _audio_callback(self, indata, frames, time, status) -> None:
        """Sounddevice callback — pushes audio chunks to the queue."""
        if status:
            logger.warning(f"Audio callback status: {status}")
        self._audio_queue.put(indata.copy())

    @property
    def is_recording(self) -> bool:
        """Return whether the microphone is currently recording."""
        return self._recording

    def stop(self) -> None:
        """Signal the recording loop to stop."""
        self._stop_flag = True
