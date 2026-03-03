"""Microphone capture with VAD-controlled recording."""

import asyncio
import os
import platform
import queue
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from shared import MicCaptureError, STT_SAMPLE_RATE, get_logger
from stt.vad import VoiceActivityDetector

logger = get_logger("stt.mic")

_CHUNK_SAMPLES = 512        # Silero VAD requires exactly 512-sample chunks at 16kHz
_CHUNK_BYTES   = _CHUNK_SAMPLES * 4   # float32 = 4 bytes/sample → 2048 bytes/chunk
_ZERO_CHECK_CHUNKS = 10    # ~320ms of silence before detecting TCC denial


def _find_audio_capture_binary() -> Optional[str]:
    """Return path to VoiceSmithMCP.app audio-capture binary, or None.

    The binary lives alongside server.py in the install directory, inside the
    app bundle that install.sh builds.  Being a binary in VoiceSmithMCP.app
    causes macOS TCC to attribute mic permission to our bundle rather than to
    Homebrew's Python.app (which lacks NSMicrophoneUsageDescription).
    """
    # __file__ is stt/mic_capture.py — go up one level to the install root
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binary = os.path.join(
        install_dir, "VoiceSmithMCP.app", "Contents", "MacOS", "audio-capture"
    )
    return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None


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

        On macOS, uses the VoiceSmithMCP.app audio-capture binary so TCC
        attributes mic permission to our bundle.  Falls back to sounddevice
        on non-macOS or when the binary is absent.

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

        # Reset VAD state — LSTM hidden state and context window must be cleared
        # between recordings to avoid stale state from the previous session.
        vad.reset()

        audio_capture_bin = (
            _find_audio_capture_binary() if platform.system() == "Darwin" else None
        )

        if audio_capture_bin:
            return await self._record_via_subprocess(
                audio_capture_bin, vad, timeout, silence_threshold, cancel_event
            )
        return await self._record_via_sounddevice(
            vad, timeout, silence_threshold, cancel_event
        )

    # ── Subprocess backend (macOS, VoiceSmithMCP.app binary) ──────────────────

    async def _record_via_subprocess(
        self,
        binary: str,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
    ) -> Optional[np.ndarray]:
        """Record using the CoreAudio binary inside VoiceSmithMCP.app."""
        self._recording = True
        self._stop_flag = False
        self._audio_queue = queue.Queue()

        try:
            proc = subprocess.Popen(
                [binary],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except Exception as e:
            self._recording = False
            raise MicCaptureError(f"Failed to start audio-capture: {e}") from e

        logger.info("Microphone recording started (audio-capture subprocess)")

        def _reader() -> None:
            """Background thread: reads stdout → audio_queue."""
            try:
                while True:
                    data = proc.stdout.read(_CHUNK_BYTES)
                    if not data or len(data) < _CHUNK_BYTES:
                        break
                    self._audio_queue.put(np.frombuffer(data, dtype=np.float32).copy())
            except Exception as exc:
                logger.debug(f"audio-capture reader thread exiting: {exc}")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            # Flush the first ~200ms to discard speaker bleed from TTS playback
            # that finished just before listen() was called.
            flush_chunks = int(0.2 * self._sample_rate / _CHUNK_SAMPLES)  # ~6
            for _ in range(flush_chunks):
                try:
                    self._audio_queue.get(timeout=0.15)
                except queue.Empty:
                    break

            return await self._run_vad_loop(vad, timeout, silence_threshold, cancel_event)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()
            reader_thread.join(timeout=1)
            self._recording = False

    # ── sounddevice backend (non-macOS fallback) ───────────────────────────────

    async def _record_via_sounddevice(
        self,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
    ) -> Optional[np.ndarray]:
        """Record using sounddevice / PortAudio (fallback for non-macOS)."""
        try:
            import sounddevice as sd
        except Exception as e:
            raise MicCaptureError(f"Failed to import sounddevice: {e}") from e

        self._recording = True
        self._stop_flag = False
        self._audio_queue = queue.Queue()

        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                blocksize=_CHUNK_SAMPLES,
                callback=self._audio_callback,
            )
            stream.start()
            logger.info("Microphone recording started (sounddevice)")

            # Discard the first ~200ms to avoid picking up residual speaker output
            # (Tink sound or TTS playback that just finished).
            flush_chunks = int(0.2 * self._sample_rate / _CHUNK_SAMPLES)  # ~6
            for _ in range(flush_chunks):
                try:
                    self._audio_queue.get(timeout=0.1)
                except queue.Empty:
                    break

            return await self._run_vad_loop(vad, timeout, silence_threshold, cancel_event)
        except MicCaptureError:
            raise
        except Exception as e:
            raise MicCaptureError(f"Recording failed: {e}") from e
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    time.sleep(0.05)  # Let CoreAudio IO thread finish (avoid segfault)
                    stream.close()
                except Exception as e:
                    logger.debug(f"Stream teardown: {e}")
            self._recording = False

    # ── Shared VAD loop ────────────────────────────────────────────────────────

    async def _run_vad_loop(
        self,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
    ) -> Optional[np.ndarray]:
        """VAD recording loop — shared by both capture backends.

        Reads 512-sample float32 chunks from self._audio_queue, runs Silero VAD
        on each, and returns when silence_threshold is exceeded after speech,
        timeout elapses, or cancel_event fires.

        Raises:
            MicCaptureError: If audio is all-zeros (TCC denial detected).
        """
        loop = asyncio.get_running_loop()
        chunks: list[np.ndarray] = []
        speech_detected = False
        silence_duration = 0.0
        zero_check_done = False
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

            # Get audio chunk (0.1s poll keeps cancel/timeout checks responsive)
            try:
                chunk = await loop.run_in_executor(
                    None, self._audio_queue.get, True, 0.1
                )
            except queue.Empty:
                continue

            chunks.append(chunk)

            # Early detection: if first N chunks are all zeros, the OS is
            # silently blocking mic access (macOS TCC denial).
            if not zero_check_done and len(chunks) >= _ZERO_CHECK_CHUNKS:
                zero_check_done = True
                if all(np.max(np.abs(c)) == 0.0 for c in chunks):
                    raise MicCaptureError(self._zero_audio_message())

            is_speech = vad.is_speech(chunk)

            if is_speech:
                speech_detected = True
                silence_duration = 0.0
            elif speech_detected:
                silence_duration += len(chunk) / self._sample_rate
                if silence_duration >= silence_threshold:
                    logger.info(
                        f"Silence threshold reached ({silence_threshold}s), stopping"
                    )
                    break

        if not chunks or not speech_detected:
            return None

        return np.concatenate(chunks).flatten()

    # ── sounddevice callback ───────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Sounddevice callback — pushes audio chunks to the queue."""
        if status:
            logger.warning(f"Audio callback status: {status}")
        self._audio_queue.put(indata.copy())

    # ── Error message ──────────────────────────────────────────────────────────

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

    # ── Properties / control ──────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        """Return whether the microphone is currently recording."""
        return self._recording

    def stop(self) -> None:
        """Signal the recording loop to stop."""
        self._stop_flag = True
