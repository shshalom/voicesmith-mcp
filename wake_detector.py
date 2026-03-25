#!/usr/bin/env python3
"""
Wake word detector — standalone process for VoiceSmith menu bar app.

Connects to the audio-service Unix socket for mic audio, runs openWakeWord,
records speech after wake, transcribes via a session's /transcribe endpoint,
and injects text into the target IDE session via tmux.

Communicates with the menu bar app via stdout events:
  LISTENING       — monitoring for wake phrase
  WAKE            — wake phrase detected
  RECORDING       — recording user speech
  TRANSCRIBING    — sending audio for transcription
  INJECTED <name> — text injected into session <name>
  YIELDED         — socket disconnected (AI took mic)
  RESUMED         — reconnected after yield
  ERROR <msg>     — something went wrong

Usage:
  python3 wake_detector.py [--model NAME] [--threshold FLOAT] [--socket PATH]
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

# ─── Constants ────────────────────────────────────────────────────────────────

AUDIO_SOCKET = "/tmp/voicesmith-audio.sock"
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * 4
WAKE_FRAME_SIZE = 1280
STT_SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 1.5
RECORDING_TIMEOUT = 15
NO_SPEECH_TIMEOUT = 8
RECONNECT_DELAY = 1.0
READY_SOUND = "/System/Library/Sounds/Tink.aiff"

DATA_DIR = Path.home() / ".local" / "share" / "voicesmith-mcp"
SESSIONS_FILE = DATA_DIR / "sessions.json"


def emit(event: str, detail: str = ""):
    line = f"{event} {detail}".strip() if detail else event
    print(line, flush=True)


def find_wake_model(name: str) -> Optional[str]:
    for p in [
        os.path.join(os.path.dirname(__file__), "models", f"{name}.onnx"),
        str(DATA_DIR / "models" / f"{name}.onnx"),
    ]:
        if os.path.exists(p):
            return p
    return None


def read_sessions() -> list:
    if not SESSIONS_FILE.exists():
        return []
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f).get("sessions", [])
    except (json.JSONDecodeError, OSError):
        return []


def is_any_session_listening() -> bool:
    """Check if any session has its mic active (AI listening)."""
    import urllib.request
    for s in read_sessions():
        port = s.get("port")
        if port:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as resp:
                    status = json.loads(resp.read())
                    if status.get("listening"):
                        return True
            except Exception:
                continue
    return False


def find_active_session_port() -> Optional[int]:
    import urllib.request
    for s in read_sessions():
        port = s.get("port")
        if port:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1):
                    return port
            except Exception:
                continue
    return None


def transcribe_audio(audio: np.ndarray, port: int) -> Optional[dict]:
    """Try HTTP /transcribe first, fall back to local Whisper."""
    import urllib.request
    # Try HTTP endpoint (new servers)
    try:
        url = f"http://127.0.0.1:{port}/transcribe"
        req = urllib.request.Request(url, data=audio.astype(np.float32).tobytes(), method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        pass

    # Fallback: transcribe locally
    return transcribe_local(audio)


_whisper_model = None

def transcribe_local(audio: np.ndarray) -> Optional[dict]:
    """Transcribe using faster-whisper directly in this process."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
        import math

        if _whisper_model is None:
            emit("ERROR", "loading whisper model...")
            _whisper_model = WhisperModel("base", compute_type="int8")

        segments, info = _whisper_model.transcribe(audio, language="en")
        text_parts = []
        total_logprob = 0
        count = 0
        for seg in segments:
            text_parts.append(seg.text)
            total_logprob += seg.avg_logprob
            count += 1

        text = " ".join(text_parts).strip()
        confidence = math.exp(total_logprob / max(count, 1)) if count > 0 else 0

        return {"success": True, "text": text, "confidence": round(confidence, 3)}
    except Exception as e:
        emit("ERROR", f"local transcribe failed: {e}")
        return None


def deliver_message(text: str):
    """Deliver transcribed wake message to the target session via HTTP."""
    import urllib.request
    sessions = read_sessions()
    message = text
    target_name = "unknown"
    target_port = None

    # Multi-session routing: parse first word as session name
    if len(sessions) > 1:
        words = text.split(None, 1)
        if words:
            first_word = words[0].strip(".,!?:")
            for s in sessions:
                if first_word.lower() == s.get("name", "").lower():
                    target_name = s["name"]
                    target_port = s.get("port")
                    message = words[1] if len(words) > 1 else ""
                    break

    # Default to most recently active session
    if target_port is None and sessions:
        # Find the one with lowest last_tool_call_age (most active)
        best = sessions[-1]
        for s in sessions:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{s['port']}/status", timeout=1) as resp:
                    status = json.loads(resp.read())
                    age = status.get("last_tool_call_age_s", 999999)
                    if age < 999999:
                        best = s
            except Exception:
                continue
        target_port = best.get("port")
        target_name = best.get("name", "unknown")

    if not message.strip():
        emit("ERROR", "empty message after name parsing")
        return

    if target_port is None:
        emit("ERROR", "no active session to deliver to")
        return

    # Deliver via HTTP POST /wake_message
    try:
        url = f"http://127.0.0.1:{target_port}/wake_message"
        body = json.dumps({"text": message}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                emit("DELIVERED", target_name)
            else:
                emit("ERROR", f"delivery failed: {result}")
    except Exception as e:
        emit("ERROR", f"delivery failed: {e}")


def play_ready_sound():
    if os.path.exists(READY_SOUND):
        try:
            subprocess.Popen(["afplay", READY_SOUND],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ─── Socket helpers ───────────────────────────────────────────────────────────

def connect_socket(socket_path: str) -> Optional[socket.socket]:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        emit("ERROR", f"connected to {socket_path}")
        return sock
    except OSError as e:
        emit("ERROR", f"socket connect failed: {e}")
        return None


def read_chunk(sock: socket.socket) -> Optional[np.ndarray]:
    data = b""
    while len(data) < CHUNK_BYTES:
        got = sock.recv(CHUNK_BYTES - len(data))
        if not got:
            return None
        data += got
    return np.frombuffer(data, dtype=np.float32).copy()


# ─── VAD ──────────────────────────────────────────────────────────────────────

def load_vad():
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from stt.vad import VoiceActivityDetector
        return VoiceActivityDetector(threshold=0.3)
    except Exception as e:
        emit("ERROR", f"VAD load failed: {e}")
        return None


def record_speech_from_sock(sock, vad) -> Optional[np.ndarray]:
    """Record speech from an already-connected socket using VAD."""
    vad.reset()
    audio_chunks = []
    speech_started = False
    silence_start = None
    record_start = time.time()

    # Flush 2 chunks
    for _ in range(2):
        read_chunk(sock)

    # Mic is ready — play Tink and show indicator
    emit("WAKE")
    emit("RECORDING")
    play_ready_sound()

    try:
        while True:
            elapsed = time.time() - record_start
            if elapsed > RECORDING_TIMEOUT:
                break

            chunk = read_chunk(sock)
            if chunk is None:
                emit("YIELDED")
                return None

            is_speech = vad.is_speech(chunk)

            if is_speech:
                speech_started = True
                silence_start = None
                audio_chunks.append(chunk)
            elif speech_started:
                audio_chunks.append(chunk)
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_THRESHOLD:
                    break
            else:
                if elapsed > NO_SPEECH_TIMEOUT:
                    return None
    except Exception:
        return None

    return np.concatenate(audio_chunks) if audio_chunks else None


def record_speech(socket_path: str, vad) -> Optional[np.ndarray]:
    """Record speech from socket using VAD."""
    sock = connect_socket(socket_path)
    if sock is None:
        emit("ERROR", "cannot reconnect for recording")
        return None

    vad.reset()
    audio_chunks = []
    speech_started = False
    silence_start = None
    record_start = time.time()

    # Flush 2 chunks for hardware settle
    for _ in range(2):
        read_chunk(sock)

    # NOW the mic is ready — play Tink and show indicator
    emit("WAKE")
    emit("RECORDING")
    play_ready_sound()

    try:
        while True:
            elapsed = time.time() - record_start
            if elapsed > RECORDING_TIMEOUT:
                break

            chunk = read_chunk(sock)
            if chunk is None:
                emit("YIELDED")
                return None

            is_speech = vad.is_speech(chunk)

            if is_speech:
                speech_started = True
                silence_start = None
                audio_chunks.append(chunk)
            elif speech_started:
                audio_chunks.append(chunk)
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_THRESHOLD:
                    break
            else:
                if elapsed > NO_SPEECH_TIMEOUT:
                    return None
    finally:
        sock.close()

    return np.concatenate(audio_chunks) if audio_chunks else None


# ─── Main loop ────────────────────────────────────────────────────────────────

class _BreakToReconnect(Exception):
    pass


def run(model_name: str, threshold: float, socket_path: str):
    from openwakeword.model import Model

    model_path = find_wake_model(model_name)
    try:
        if model_path:
            wake_model = Model(wakeword_models=[model_path], inference_framework="onnx")
        else:
            wake_model = Model(wakeword_models=[model_name], inference_framework="onnx")
    except Exception as e:
        emit("ERROR", f"wake model load failed: {e}")
        sys.exit(1)

    vad = load_vad()
    if vad is None:
        sys.exit(1)

    last_wake_time = 0  # debounce: ignore detections for 5s after a wake

    while True:
        sock = connect_socket(socket_path)
        if sock is None:
            time.sleep(RECONNECT_DELAY)
            continue

        emit("LISTENING")

        frame_buffer = np.array([], dtype=np.float32)

        # After reconnect, flush the model's internal state by feeding
        # 3 seconds of audio without checking predictions. This clears
        # residual activation from the previous "Hey Jarvis".
        flush_until = time.time() + 3 if last_wake_time > 0 else 0

        try:
            while True:
                chunk = read_chunk(sock)
                if chunk is None:
                    emit("YIELDED")
                    break

                frame_buffer = np.concatenate([frame_buffer, chunk])

                while len(frame_buffer) >= WAKE_FRAME_SIZE:
                    frame_f32 = frame_buffer[:WAKE_FRAME_SIZE]
                    frame_buffer = frame_buffer[WAKE_FRAME_SIZE:]
                    frame_i16 = (frame_f32 * 32767).astype(np.int16)

                    # Feed audio to model (always, to keep state current)
                    prediction = wake_model.predict(frame_i16)

                    # Skip detection during flush period (clears model state)
                    if time.time() < flush_until:
                        continue

                    detected = False
                    for name, score in prediction.items():
                        if score > threshold:
                            detected = True
                            break

                    if detected:
                        last_wake_time = time.time()
                        # Don't close socket — reuse it for recording
                        # Just clear the frame buffer
                        frame_buffer = np.array([], dtype=np.float32)

                        # Record speech using the SAME socket connection
                        audio = record_speech_from_sock(sock, vad)

                        if audio is not None and len(audio) > 0:
                            emit("TRANSCRIBING")
                            port = find_active_session_port()
                            if port:
                                result = transcribe_audio(audio, port)
                                if result and result.get("success") and result.get("text"):
                                    deliver_message(result["text"])
                                else:
                                    emit("ERROR", "transcription failed or empty")
                            else:
                                emit("ERROR", "no active session")

                        emit("LISTENING")
                        raise _BreakToReconnect()

        except _BreakToReconnect:
            continue
        except Exception as e:
            emit("ERROR", str(e))

        try:
            sock.close()
        except Exception:
            pass

        # Wait until no session is actively listening before reconnecting
        while is_any_session_listening():
            time.sleep(0.5)

        time.sleep(RECONNECT_DELAY)
        emit("RESUMED")
        emit("LISTENING")


def main():
    parser = argparse.ArgumentParser(description="VoiceSmith wake word detector")
    parser.add_argument("--model", default="hey_jarvis_v0.1", help="Wake word model name")
    parser.add_argument("--threshold", type=float, default=0.5, help="Detection threshold")
    parser.add_argument("--socket", default=AUDIO_SOCKET, help="Audio service socket path")
    args = parser.parse_args()
    run(args.model, args.threshold, args.socket)


if __name__ == "__main__":
    main()
