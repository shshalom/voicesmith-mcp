"""
Wake word listener for user-initiated voice input.

Runs as a daemon thread inside the MCP server. Continuously monitors the
microphone for a wake phrase (via openWakeWord), then records speech,
transcribes with Whisper, and injects the text into the target IDE session
via tmux send-keys.
"""

import os
import platform
import queue
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np

from shared import (
    STT_SAMPLE_RATE,
    WAKE_WORD_FRAME_SIZE,
    DEFAULT_WAKE_THRESHOLD,
    DEFAULT_RECORDING_TIMEOUT,
    DEFAULT_NO_SPEECH_TIMEOUT,
    READY_SOUND,
    SESSIONS_FILE_NAME,
    get_logger,
)

logger = get_logger("wake-listener")


class WakeState(Enum):
    DISABLED = "disabled"
    LISTENING = "listening"
    RECORDING = "recording"
    YIELDED = "yielded"


class WakeWordListener:
    """Continuous wake word listener with mic ownership management."""

    def __init__(
        self,
        stt_engine,
        vad,
        wake_model_name: str = "hey_jarvis_v0.1",
        threshold: float = DEFAULT_WAKE_THRESHOLD,
        tmux_session: Optional[str] = None,
        ready_sound: str = "tink",
        recording_timeout: float = DEFAULT_RECORDING_TIMEOUT,
        no_speech_timeout: float = DEFAULT_NO_SPEECH_TIMEOUT,
    ):
        self._stt_engine = stt_engine
        self._vad = vad
        self._threshold = threshold
        self._tmux_session = tmux_session
        self._ready_sound = self._resolve_sound(ready_sound)
        self._recording_timeout = recording_timeout
        self._no_speech_timeout = no_speech_timeout

        # State
        self._state = WakeState.DISABLED
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._yield_event = threading.Event()
        self._yield_done = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Load openWakeWord model
        self._wake_model = None
        try:
            from openwakeword.model import Model
            self._wake_model = Model(
                wakeword_models=[wake_model_name],
                inference_framework="onnx",
            )
            logger.info(f"Wake word model loaded: {wake_model_name}")
        except Exception as e:
            logger.error(f"Failed to load wake word model: {e}")

    @staticmethod
    def _resolve_sound(sound: str) -> Optional[str]:
        """Resolve a sound name to a file path."""
        if sound is None or sound == "":
            return None
        if sound == "tink":
            if platform.system() == "Darwin":
                return "/System/Library/Sounds/Tink.aiff"
            return None  # Linux: no default sound bundled yet
        if os.path.exists(sound):
            return sound
        return None

    def start(self):
        """Start the wake word listener thread."""
        if self._wake_model is None:
            logger.warning("Cannot start wake listener: model not loaded")
            return
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Wake listener already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.info("Wake word listener started")

    def stop(self):
        """Stop the wake word listener."""
        self._stop_event.set()
        with self._state_lock:
            self._state = WakeState.DISABLED
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Wake word listener stopped")

    def yield_mic(self):
        """Pause listening and release the mic for the AI listen tool."""
        if self._state != WakeState.LISTENING:
            return
        self._yield_event.set()
        # Wait for the listener to actually pause
        self._yield_done.wait(timeout=3)
        logger.debug("Wake listener yielded mic")

    def reclaim_mic(self):
        """Resume listening after the AI listen tool is done."""
        self._yield_event.clear()
        self._yield_done.clear()
        logger.debug("Wake listener reclaiming mic")

    @property
    def is_listening(self) -> bool:
        return self._state == WakeState.LISTENING

    @property
    def state(self) -> str:
        return self._state.value

    def _listen_loop(self):
        """Main loop: continuously listen for the wake word."""
        import sounddevice as sd

        with self._state_lock:
            self._state = WakeState.LISTENING

        while not self._stop_event.is_set():
            # Check for yield request
            if self._yield_event.is_set():
                with self._state_lock:
                    self._state = WakeState.YIELDED
                self._yield_done.set()
                # Wait until reclaim
                while self._yield_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.1)
                if self._stop_event.is_set():
                    break
                with self._state_lock:
                    self._state = WakeState.LISTENING
                continue

            # Open mic for wake word detection
            audio_queue = queue.Queue()

            def callback(indata, frames, time_info, status):
                audio_queue.put(indata[:, 0].copy())

            try:
                stream = sd.InputStream(
                    samplerate=STT_SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=WAKE_WORD_FRAME_SIZE,
                    callback=callback,
                )
                stream.start()
            except Exception as e:
                logger.error(f"Failed to open mic for wake word: {e}")
                time.sleep(1)
                continue

            try:
                while not self._stop_event.is_set() and not self._yield_event.is_set():
                    try:
                        chunk = audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    prediction = self._wake_model.predict(chunk)
                    for name, score in prediction.items():
                        if score > self._threshold:
                            logger.info(f"Wake word detected: {name} ({score:.3f})")
                            stream.stop()
                            stream.close()
                            self._handle_wake_detected()
                            # Break out to reopen stream for next detection
                            raise _WakeDetected()

            except _WakeDetected:
                continue  # Restart the outer loop to reopen the stream
            except Exception as e:
                logger.error(f"Wake listener error: {e}")
                time.sleep(1)
            finally:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

        with self._state_lock:
            self._state = WakeState.DISABLED

    def _handle_wake_detected(self):
        """Record speech after wake word, transcribe, and inject."""
        import sounddevice as sd

        with self._state_lock:
            self._state = WakeState.RECORDING

        # Flush and wait for audio system to settle
        time.sleep(0.15)

        # Play ready sound
        if self._ready_sound:
            try:
                if platform.system() == "Darwin":
                    subprocess.run(
                        ["afplay", self._ready_sound],
                        capture_output=True,
                        timeout=2,
                    )
                else:
                    subprocess.run(
                        ["aplay", self._ready_sound],
                        capture_output=True,
                        timeout=2,
                    )
            except Exception:
                pass

        # Record speech with VAD
        audio_queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            audio_queue.put(indata.copy())

        # Retry stream open up to 3 times
        stream = None
        for attempt in range(3):
            try:
                stream = sd.InputStream(
                    samplerate=STT_SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=512,
                    callback=callback,
                )
                stream.start()
                break
            except Exception as e:
                logger.warning(f"Mic open attempt {attempt + 1} failed: {e}")
                time.sleep(0.2)

        if stream is None:
            logger.error("Failed to open mic for recording after 3 attempts")
            with self._state_lock:
                self._state = WakeState.LISTENING
            return

        # Reset VAD state
        self._vad.reset()

        chunks = []
        speech_detected = False
        silence_duration = 0.0
        start_time = time.time()

        try:
            while True:
                elapsed = time.time() - start_time

                # Max recording timeout
                if elapsed >= self._recording_timeout:
                    logger.info("Recording timeout reached")
                    break

                # No speech timeout
                if not speech_detected and elapsed >= self._no_speech_timeout:
                    logger.info("No speech after wake word — aborting")
                    stream.stop()
                    stream.close()
                    with self._state_lock:
                        self._state = WakeState.LISTENING
                    return

                try:
                    chunk = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                chunks.append(chunk)
                flat = chunk.flatten()
                is_speech = self._vad.is_speech(flat)

                if is_speech:
                    speech_detected = True
                    silence_duration = 0.0
                elif speech_detected:
                    chunk_duration = len(flat) / STT_SAMPLE_RATE
                    silence_duration += chunk_duration
                    if silence_duration >= 1.5:
                        logger.info("Silence detected — stopping recording")
                        break
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        if not speech_detected or not chunks:
            logger.info("No speech captured")
            with self._state_lock:
                self._state = WakeState.LISTENING
            return

        # Transcribe
        audio = np.concatenate(chunks).flatten()
        logger.info(f"Transcribing {len(audio) / STT_SAMPLE_RATE:.1f}s of audio...")

        try:
            result = self._stt_engine.transcribe(audio, STT_SAMPLE_RATE)
            text = result.text.strip()
            logger.info(f"Transcribed: '{text}' (confidence: {result.confidence:.3f})")
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            with self._state_lock:
                self._state = WakeState.LISTENING
            return

        if not text:
            logger.info("Empty transcription")
            with self._state_lock:
                self._state = WakeState.LISTENING
            return

        # Inject into session
        self._inject_text(text)

        with self._state_lock:
            self._state = WakeState.LISTENING

    def _inject_text(self, text: str):
        """Route transcribed text to the correct tmux session."""
        from session_registry import get_active_sessions

        sessions = get_active_sessions()
        # Filter to sessions with tmux
        tmux_sessions = [s for s in sessions if s.get("tmux_session")]

        if not tmux_sessions:
            logger.warning("No tmux sessions available for text injection")
            return

        target_tmux = None
        message = text

        if len(tmux_sessions) == 1:
            # Single session — send everything
            target_tmux = tmux_sessions[0]["tmux_session"]
        else:
            # Multiple sessions — parse first word as session name
            words = text.split(None, 1)
            if len(words) >= 1:
                first_word = words[0].strip(".,!?:")
                for s in tmux_sessions:
                    if first_word.lower() == s["name"].lower():
                        target_tmux = s["tmux_session"]
                        message = words[1] if len(words) > 1 else ""
                        break

            if target_tmux is None:
                # No name match — use most recent session
                target_tmux = tmux_sessions[-1]["tmux_session"]

        if not message.strip():
            logger.info("Empty message after name parsing — skipping injection")
            return

        # Send via tmux (literal mode to prevent shell injection)
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", target_tmux, "-l", message],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", target_tmux, "Enter"],
                capture_output=True,
                timeout=5,
            )
            logger.info(f"Injected text into tmux session '{target_tmux}'")
        except subprocess.TimeoutExpired:
            logger.error("tmux send-keys timed out")
        except FileNotFoundError:
            logger.error("tmux not found — cannot inject text")
        except Exception as e:
            logger.error(f"tmux injection failed: {e}")


class _WakeDetected(Exception):
    """Internal signal for breaking out of nested loops on wake detection."""
    pass
