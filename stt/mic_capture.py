"""Microphone capture with VAD-controlled recording."""

import asyncio
import platform
import queue
import time
from typing import Optional

import numpy as np

from shared import MicCaptureError, STT_SAMPLE_RATE, get_logger
from stt.vad import VoiceActivityDetector

logger = get_logger("stt.mic")

# Number of initial chunks to check for zero-amplitude audio (TCC silent denial)
_ZERO_CHECK_CHUNKS = 10  # ~320ms at 512 samples/16kHz


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
        zero_check_done = False
        loop = asyncio.get_running_loop()

        # Reset VAD state — the LSTM hidden state and context window must
        # be cleared between recordings to avoid stale state from previous
        # audio affecting speech detection.
        vad.reset()

        stream = None
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

            # Discard the first ~200ms of audio to avoid picking up residual
            # speaker output (Tink sound or TTS playback that just finished).
            # This prevents VAD from detecting speaker bleed as "speech" and
            # then cutting off when the bleed stops.
            flush_chunks = int(0.2 * self._sample_rate / 512)  # ~6 chunks
            for _ in range(flush_chunks):
                try:
                    self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    break

            start_time = loop.time()

            while not self._stop_flag:
                # Check cancellation
                if cancel_event and cancel_event.is_set():
                    logger.info("Recording cancelled by event")
                    break

                # Check timeout
                elapsed = loop.time() - start_time
                if elapsed >= timeout:
                    if not speech_detected:
                        logger.info("Recording timed out with no speech detected")
                    else:
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

                # Early detection: if first N chunks are all zeros, the OS
                # is silently blocking mic access (macOS TCC denial).
                if not zero_check_done and len(chunks) >= _ZERO_CHECK_CHUNKS:
                    zero_check_done = True
                    all_zero = all(
                        np.max(np.abs(c)) == 0.0 for c in chunks
                    )
                    if all_zero:
                        raise MicCaptureError(
                            self._zero_audio_message()
                        )

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

            if not chunks or not speech_detected:
                return None

            return np.concatenate(chunks).flatten()

        except MicCaptureError:
            raise
        except Exception as e:
            raise MicCaptureError(f"Recording failed: {e}") from e
        finally:
            # Safely tear down the audio stream. The CoreAudio IO thread may
            # still be executing the callback when we call stop(). Wait briefly
            # between stop() and close() to let the IO thread finish — this
            # prevents the segfault in libffi/PortAudio where the callback
            # dereferences freed memory.
            if stream is not None:
                try:
                    stream.stop()
                    time.sleep(0.05)  # Let CoreAudio IO thread finish
                    stream.close()
                except Exception as e:
                    logger.debug(f"Stream teardown: {e}")
            self._recording = False

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Sounddevice callback — pushes audio chunks to the queue."""
        if status:
            logger.warning(f"Audio callback status: {status}")
        self._audio_queue.put(indata.copy())

    @staticmethod
    def _zero_audio_message() -> str:
        """Build an error message for zero-amplitude mic input."""
        msg = (
            "Microphone is returning silent audio. "
            "The audio stream opened successfully but every sample is zero."
        )
        if platform.system() == "Darwin":
            msg += (
                "\n\nmacOS silently blocks mic access when the parent terminal app "
                "hasn't been granted Microphone permission. Go to:\n"
                "  System Settings > Privacy & Security > Microphone\n"
                "and enable the toggle for the terminal app running Claude Code "
                "(e.g. Terminal, iTerm2, Ghostty, Warp, etc.).\n\n"
                "If your terminal isn't listed, try running a mic-using app from it "
                "to trigger the permission prompt, or add it manually via:\n"
                "  tccutil reset Microphone"
            )
        return msg

    @property
    def is_recording(self) -> bool:
        """Return whether the microphone is currently recording."""
        return self._recording

    def stop(self) -> None:
        """Signal the recording loop to stop."""
        self._stop_flag = True
