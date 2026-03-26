"""Microphone capture with VAD-controlled recording."""

import asyncio
import os
import platform
import queue
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

import numpy as np

from shared import MicCaptureError, STT_SAMPLE_RATE, get_logger
from stt.vad import VoiceActivityDetector
from tts.media_duck import is_bluetooth_output

logger = get_logger("stt.mic")

_CHUNK_SAMPLES = 512        # Silero VAD requires exactly 512-sample chunks at 16kHz
_CHUNK_BYTES   = _CHUNK_SAMPLES * 4   # float32 = 4 bytes/sample → 2048 bytes/chunk
_ZERO_CHECK_CHUNKS = 25    # ~800ms — exceeds CoreAudio cold-start latency (~544ms)
_ZERO_CHECK_CHUNKS_BT = 75 # ~2.4s — Bluetooth A2DP→HFP codec switch can take 1-2s

_AUDIO_SERVICE_SOCKET  = "/tmp/voicesmith-audio.sock"
_LAUNCHAGENT_LABEL     = "com.voicesmith-mcp.audio"
_LAUNCHAGENT_PLIST     = os.path.expanduser(
    f"~/Library/LaunchAgents/{_LAUNCHAGENT_LABEL}.plist"
)


def _find_app_binary(name: str) -> Optional[str]:
    """Return path to a named binary inside VoiceSmithMCP.app, or None."""
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binary = os.path.join(install_dir, "VoiceSmithMCP.app", "Contents", "MacOS", name)
    return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None


def _launchagent_available() -> bool:
    """Return True if the VoiceSmithMCP audio LaunchAgent plist is installed."""
    return os.path.isfile(_LAUNCHAGENT_PLIST)


def _ensure_audio_service_running() -> None:
    """Start the audio LaunchAgent if it is not already running.

    The service is started via launchctl.  We then wait up to 3 seconds for
    the Unix socket to appear, which signals the service is ready to accept
    connections.
    """
    # If the socket exists and is connectable, service is already running.
    if _socket_ready():
        return

    logger.info("Starting audio service via launchctl")
    try:
        subprocess.run(
            ["launchctl", "start", _LAUNCHAGENT_LABEL],
            capture_output=True,
            timeout=5,
        )
    except Exception as e:
        raise MicCaptureError(f"Failed to start audio service: {e}") from e

    # Wait up to 3 s for the socket to appear.
    for _ in range(30):
        if _socket_ready():
            return
        time.sleep(0.1)
    raise MicCaptureError(
        "VoiceSmith audio service did not start in time.  "
        f"Check {_LAUNCHAGENT_PLIST} and launchctl output."
    )


def _socket_ready() -> bool:
    """Return True if the audio service socket file exists."""
    return os.path.exists(_AUDIO_SERVICE_SOCKET)


class MicCapture:
    """Microphone capture with voice activity detection."""

    def __init__(self, sample_rate: int = STT_SAMPLE_RATE, audio_input_device: int | None = None) -> None:
        self._sample_rate = sample_rate
        self._audio_input_device = audio_input_device
        self._recording = False
        self._audio_queue: queue.Queue = queue.Queue()
        self._stop_flag = False

    async def record(
        self,
        vad: VoiceActivityDetector,
        timeout: float = 15,
        silence_threshold: float = 1.5,
        cancel_event: Optional[asyncio.Event] = None,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> Optional[np.ndarray]:
        """Record audio from the microphone until silence is detected.

        On macOS, prefers the audio-service LaunchAgent backend which runs
        under launchd (ppid=1), ensuring macOS TCC attributes mic permission
        to VoiceSmithMCP.app rather than to the user's terminal app.
        Falls back to the audio-capture subprocess if the LaunchAgent is not
        installed, and to sounddevice on non-macOS systems.

        Args:
            vad: VoiceActivityDetector instance for speech detection.
            timeout: Maximum seconds to wait for speech (default 15).
            silence_threshold: Seconds of silence before stopping (default 1.5).
            cancel_event: Optional asyncio.Event to cancel recording.
            on_ready: Optional callback invoked once the mic is live and
                      ready to capture.  Called after hardware warm-up /
                      flush but before the VAD loop starts.

        Returns:
            Numpy array of recorded audio, or None if cancelled/timeout.

        Raises:
            MicCaptureError: If microphone access fails.
        """
        if self._recording:
            raise MicCaptureError("Another recording is already in progress")

        # Reset VAD state between recordings.
        vad.reset()

        if platform.system() == "Darwin":
            if _launchagent_available():
                return await self._record_via_socket(
                    vad, timeout, silence_threshold, cancel_event, on_ready
                )
            # Legacy: subprocess fallback for installs without the LaunchAgent.
            audio_capture_bin = _find_app_binary("audio-service") or _find_app_binary("audio-capture")
            if audio_capture_bin:
                return await self._record_via_subprocess(
                    audio_capture_bin, vad, timeout, silence_threshold, cancel_event, on_ready
                )

        return await self._record_via_sounddevice(
            vad, timeout, silence_threshold, cancel_event, on_ready
        )

    # ── LaunchAgent socket backend (macOS primary) ─────────────────────────────

    async def _record_via_socket(
        self,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
        on_ready: Optional[Callable[[], None]] = None,
    ) -> Optional[np.ndarray]:
        """Record via the VoiceSmithMCP audio LaunchAgent (Unix socket).

        The LaunchAgent runs under launchd so macOS TCC attributes mic access
        to com.voicesmith-mcp.launcher, not to the parent terminal app.
        """
        loop = asyncio.get_running_loop()

        # Ensure the service is up and the socket is ready.
        try:
            await loop.run_in_executor(None, _ensure_audio_service_running)
        except MicCaptureError:
            raise
        except Exception as e:
            raise MicCaptureError(f"Audio service error: {e}") from e

        # Open socket connection.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(_AUDIO_SERVICE_SOCKET)
        except OSError as e:
            sock.close()
            raise MicCaptureError(f"Cannot connect to audio service: {e}") from e

        self._recording = True
        self._stop_flag = False
        self._audio_queue = queue.Queue()

        def _reader() -> None:
            """Background thread: reads socket chunks → audio_queue."""
            try:
                while True:
                    data = b""
                    while len(data) < _CHUNK_BYTES:
                        got = sock.recv(_CHUNK_BYTES - len(data))
                        if not got:
                            return  # service closed connection
                        data += got
                    self._audio_queue.put(np.frombuffer(data, dtype=np.float32).copy())
            except Exception as exc:
                logger.debug(f"socket reader thread exiting: {exc}")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        logger.info("Microphone recording started (audio-service socket)")

        try:
            # Flush 2 chunks (~64ms) for AudioQueue hardware settle.
            self._flush_queue(2)
            if on_ready:
                on_ready()
            return await self._run_vad_loop(vad, timeout, silence_threshold, cancel_event)
        finally:
            sock.close()  # signals service to stop sending for this session
            reader_thread.join(timeout=1)
            self._recording = False

    # ── Subprocess backend (macOS legacy fallback) ─────────────────────────────

    async def _record_via_subprocess(
        self,
        binary: str,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
        on_ready: Optional[Callable[[], None]] = None,
    ) -> Optional[np.ndarray]:
        """Record using a CoreAudio binary inside VoiceSmithMCP.app (legacy)."""
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
            raise MicCaptureError(f"Failed to start audio binary: {e}") from e

        logger.info("Microphone recording started (subprocess fallback)")

        def _reader() -> None:
            try:
                while True:
                    data = proc.stdout.read(_CHUNK_BYTES)
                    if not data or len(data) < _CHUNK_BYTES:
                        break
                    self._audio_queue.put(np.frombuffer(data, dtype=np.float32).copy())
            except Exception as exc:
                logger.debug(f"subprocess reader thread exiting: {exc}")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            self._flush_queue(2)
            if on_ready:
                on_ready()
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
        on_ready: Optional[Callable[[], None]] = None,
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
            stream_kwargs = dict(
                samplerate=self._sample_rate,
                channels=1,
                dtype="float32",
                blocksize=_CHUNK_SAMPLES,
                callback=self._audio_callback,
            )
            if self._audio_input_device is not None:
                stream_kwargs["device"] = self._audio_input_device
            stream = sd.InputStream(**stream_kwargs)
            stream.start()
            logger.info("Microphone recording started (sounddevice)")

            self._flush_queue(2, chunk_timeout=0.1)
            if on_ready:
                on_ready()
            return await self._run_vad_loop(vad, timeout, silence_threshold, cancel_event)
        except MicCaptureError:
            raise
        except Exception as e:
            raise MicCaptureError(f"Recording failed: {e}") from e
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    time.sleep(0.05)
                    stream.close()
                except Exception as e:
                    logger.debug(f"Stream teardown: {e}")
            self._recording = False

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _flush_queue(self, n_chunks: int, chunk_timeout: float = 0.15) -> None:
        """Discard the first n_chunks from the audio queue (drops speaker bleed)."""
        for _ in range(n_chunks):
            try:
                self._audio_queue.get(timeout=chunk_timeout)
            except queue.Empty:
                break

    # ── Shared VAD loop ────────────────────────────────────────────────────────

    async def _run_vad_loop(
        self,
        vad: VoiceActivityDetector,
        timeout: float,
        silence_threshold: float,
        cancel_event: Optional[asyncio.Event],
    ) -> Optional[np.ndarray]:
        """VAD recording loop — shared by all capture backends.

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
        # Bluetooth A2DP→HFP switch delivers zeros for up to ~2s
        zero_threshold = _ZERO_CHECK_CHUNKS_BT if is_bluetooth_output() else _ZERO_CHECK_CHUNKS
        start_time = loop.time()

        while not self._stop_flag:
            if cancel_event and cancel_event.is_set():
                logger.info("Recording cancelled by event")
                break

            elapsed = loop.time() - start_time
            if elapsed >= timeout:
                if not speech_detected:
                    logger.info("Recording timed out with no speech detected")
                else:
                    logger.info("Recording timed out")
                break

            try:
                chunk = await loop.run_in_executor(
                    None, self._audio_queue.get, True, 0.1
                )
            except queue.Empty:
                continue

            chunks.append(chunk)

            if not zero_check_done and len(chunks) >= zero_threshold:
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
                "\n\nmacOS is blocking mic access.  The VoiceSmithMCP audio service "
                "may not have been granted Microphone permission yet.  "
                "Check System Settings > Privacy & Security > Microphone and "
                "ensure VoiceSmithMCP is enabled.\n\n"
                "If VoiceSmithMCP is not listed, re-run the installer:\n"
                "  ./install.sh"
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
