"""
Shared constants, types, and utilities for VoiceSmith MCP Server.

This module is the single source of truth for:
- Audio constants and defaults
- Voice catalog (all 53 Kokoro voices)
- Result dataclasses used across subsystems
- Custom exceptions
- Logging configuration
"""

import logging
import sys
from dataclasses import dataclass, field
from typing import Optional


# ─── Audio Constants ──────────────────────────────────────────────────────────

SAMPLE_RATE = 24000          # Kokoro TTS output sample rate
STT_SAMPLE_RATE = 16000      # Whisper/VAD input sample rate
DEFAULT_SPEED = 1.0          # Default TTS speed multiplier
MAX_CHUNK_LENGTH = 500       # Auto-chunk text longer than this (characters)
SILENCE_THRESHOLD = 1.5      # Seconds of silence before stopping recording
LISTEN_TIMEOUT = 15          # Default max seconds to wait for speech
REGISTRY_SAVE_INTERVAL = 60  # Seconds between periodic registry saves
DEFAULT_HTTP_PORT = 7865     # HTTP listener port for push-to-talk
SESSIONS_FILE_NAME = "sessions.json"
AUDIO_LOCK_PATH = "/tmp/voicesmith-audio.lock"
WAKE_MIC_LOCK_PATH = "/tmp/voicesmith-wake-mic.lock"
READY_SOUND = "/System/Library/Sounds/Tink.aiff"
WAKE_WORD_FRAME_SIZE = 1280  # openWakeWord frame size (80ms at 16kHz)
DEFAULT_WAKE_THRESHOLD = 0.5 # Wake word detection confidence threshold
DEFAULT_RECORDING_TIMEOUT = 10  # Max seconds to record after wake word
DEFAULT_NO_SPEECH_TIMEOUT = 5   # Seconds of no speech after wake before abort


# ─── Voice Catalog ────────────────────────────────────────────────────────────

# All 53 Kokoro voice IDs
ALL_VOICE_IDS: set[str] = {
    # American English - Female (11)
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    # American English - Male (9)
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa",
    # British English - Female (4)
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    # British English - Male (4)
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    # Spanish (3)
    "ef_dora", "em_alex", "em_santa",
    # French (1)
    "ff_siwis",
    # Hindi (4)
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    # Italian (2)
    "if_sara", "im_nicola",
    # Japanese (5)
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    # Portuguese (3)
    "pf_dora", "pm_alex", "pm_santa",
    # Mandarin (8)
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
}

# Map lowercase name → voice_id for auto-discovery
# Extracted from the voice ID suffix (e.g., "am_eric" → "eric": "am_eric")
VOICE_NAME_MAP: dict[str, str] = {
    # American English - Female
    "alloy": "af_alloy",
    "aoede": "af_aoede",
    "bella": "af_bella",
    "heart": "af_heart",
    "jessica": "af_jessica",
    "kore": "af_kore",
    "nicole": "af_nicole",
    "nova": "af_nova",
    "river": "af_river",
    "sarah": "af_sarah",
    "sky": "af_sky",
    # American English - Male
    "adam": "am_adam",
    "echo": "am_echo",
    "eric": "am_eric",
    "fenrir": "am_fenrir",
    "liam": "am_liam",
    "michael": "am_michael",
    "onyx": "am_onyx",
    "puck": "am_puck",
    "santa": "am_santa",  # Note: "santa" maps to am_santa (first match)
    # British English - Female
    "alice": "bf_alice",
    "emma": "bf_emma",
    "isabella": "bf_isabella",
    "lily": "bf_lily",
    # British English - Male
    "daniel": "bm_daniel",
    "fable": "bm_fable",
    "george": "bm_george",
    "lewis": "bm_lewis",
    # Spanish
    "dora": "ef_dora",  # Note: "dora" maps to ef_dora (first match)
    "alex": "em_alex",  # Note: "alex" maps to em_alex (first match)
    # French
    "siwis": "ff_siwis",
    # Hindi
    "alpha": "hf_alpha",  # Note: "alpha" maps to hf_alpha (first match)
    "beta": "hf_beta",
    "omega": "hm_omega",
    "psi": "hm_psi",
    # Italian
    "sara": "if_sara",
    "nicola": "im_nicola",
    # Japanese
    "gongitsune": "jf_gongitsune",
    "nezumi": "jf_nezumi",
    "tebukuro": "jf_tebukuro",
    "kumo": "jm_kumo",
    # Mandarin
    "xiaobei": "zf_xiaobei",
    "xiaoni": "zf_xiaoni",
    "xiaoxiao": "zf_xiaoxiao",
    "xiaoyi": "zf_xiaoyi",
    "yunjian": "zm_yunjian",
    "yunxi": "zm_yunxi",
    "yunxia": "zm_yunxia",
    "yunyang": "zm_yunyang",
}

# Voice metadata for list_voices tool
VOICE_METADATA: list[dict] = [
    # American English - Female
    {"id": "af_alloy", "gender": "female", "accent": "american"},
    {"id": "af_aoede", "gender": "female", "accent": "american"},
    {"id": "af_bella", "gender": "female", "accent": "american"},
    {"id": "af_heart", "gender": "female", "accent": "american"},
    {"id": "af_jessica", "gender": "female", "accent": "american"},
    {"id": "af_kore", "gender": "female", "accent": "american"},
    {"id": "af_nicole", "gender": "female", "accent": "american"},
    {"id": "af_nova", "gender": "female", "accent": "american"},
    {"id": "af_river", "gender": "female", "accent": "american"},
    {"id": "af_sarah", "gender": "female", "accent": "american"},
    {"id": "af_sky", "gender": "female", "accent": "american"},
    # American English - Male
    {"id": "am_adam", "gender": "male", "accent": "american"},
    {"id": "am_echo", "gender": "male", "accent": "american"},
    {"id": "am_eric", "gender": "male", "accent": "american"},
    {"id": "am_fenrir", "gender": "male", "accent": "american"},
    {"id": "am_liam", "gender": "male", "accent": "american"},
    {"id": "am_michael", "gender": "male", "accent": "american"},
    {"id": "am_onyx", "gender": "male", "accent": "american"},
    {"id": "am_puck", "gender": "male", "accent": "american"},
    {"id": "am_santa", "gender": "male", "accent": "american"},
    # British English - Female
    {"id": "bf_alice", "gender": "female", "accent": "british"},
    {"id": "bf_emma", "gender": "female", "accent": "british"},
    {"id": "bf_isabella", "gender": "female", "accent": "british"},
    {"id": "bf_lily", "gender": "female", "accent": "british"},
    # British English - Male
    {"id": "bm_daniel", "gender": "male", "accent": "british"},
    {"id": "bm_fable", "gender": "male", "accent": "british"},
    {"id": "bm_george", "gender": "male", "accent": "british"},
    {"id": "bm_lewis", "gender": "male", "accent": "british"},
    # Spanish
    {"id": "ef_dora", "gender": "female", "accent": "spanish"},
    {"id": "em_alex", "gender": "male", "accent": "spanish"},
    {"id": "em_santa", "gender": "male", "accent": "spanish"},
    # French
    {"id": "ff_siwis", "gender": "female", "accent": "french"},
    # Hindi
    {"id": "hf_alpha", "gender": "female", "accent": "hindi"},
    {"id": "hf_beta", "gender": "female", "accent": "hindi"},
    {"id": "hm_omega", "gender": "male", "accent": "hindi"},
    {"id": "hm_psi", "gender": "male", "accent": "hindi"},
    # Italian
    {"id": "if_sara", "gender": "female", "accent": "italian"},
    {"id": "im_nicola", "gender": "male", "accent": "italian"},
    # Japanese
    {"id": "jf_alpha", "gender": "female", "accent": "japanese"},
    {"id": "jf_gongitsune", "gender": "female", "accent": "japanese"},
    {"id": "jf_nezumi", "gender": "female", "accent": "japanese"},
    {"id": "jf_tebukuro", "gender": "female", "accent": "japanese"},
    {"id": "jm_kumo", "gender": "male", "accent": "japanese"},
    # Portuguese
    {"id": "pf_dora", "gender": "female", "accent": "portuguese"},
    {"id": "pm_alex", "gender": "male", "accent": "portuguese"},
    {"id": "pm_santa", "gender": "male", "accent": "portuguese"},
    # Mandarin
    {"id": "zf_xiaobei", "gender": "female", "accent": "mandarin"},
    {"id": "zf_xiaoni", "gender": "female", "accent": "mandarin"},
    {"id": "zf_xiaoxiao", "gender": "female", "accent": "mandarin"},
    {"id": "zf_xiaoyi", "gender": "female", "accent": "mandarin"},
    {"id": "zm_yunjian", "gender": "male", "accent": "mandarin"},
    {"id": "zm_yunxi", "gender": "male", "accent": "mandarin"},
    {"id": "zm_yunxia", "gender": "male", "accent": "mandarin"},
    {"id": "zm_yunyang", "gender": "male", "accent": "mandarin"},
]


# ─── Result Dataclasses ──────────────────────────────────────────────────────

@dataclass
class SynthesisResult:
    """Result from TTS engine synthesis."""
    samples: object  # numpy ndarray
    sample_rate: int
    duration_ms: float
    synthesis_ms: float


@dataclass
class PlaybackResult:
    """Result from audio player."""
    success: bool
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class SpeakResult:
    """Result from speech queue speak operation."""
    success: bool
    voice: str = ""
    auto_assigned: bool = False
    duration_ms: float = 0.0
    synthesis_ms: float = 0.0
    queued: bool = False
    error: Optional[str] = None


@dataclass
class ListenResult:
    """Result from STT listen operation."""
    success: bool
    text: str = ""
    confidence: float = 0.0
    duration_ms: float = 0.0
    transcription_ms: float = 0.0
    error: Optional[str] = None
    cancelled: bool = False


@dataclass
class TranscriptionResult:
    """Result from Whisper engine transcription."""
    text: str
    confidence: float
    transcription_ms: float
    language: str = ""


# ─── Exceptions ───────────────────────────────────────────────────────────────

class TTSEngineError(Exception):
    """Raised when TTS engine encounters an error."""
    pass


class STTEngineError(Exception):
    """Raised when STT engine encounters an error."""
    pass


class VADError(Exception):
    """Raised when Voice Activity Detection encounters an error."""
    pass


class AudioPlayerError(Exception):
    """Raised when audio playback encounters an error."""
    pass


class MicCaptureError(Exception):
    """Raised when microphone capture encounters an error."""
    pass


# ─── Logging ──────────────────────────────────────────────────────────────────

def get_logger(name: str = "voicesmith-mcp") -> logging.Logger:
    """Get a logger that outputs to stderr (MCP convention: stdout is reserved for protocol)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
