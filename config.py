"""
Configuration management for Agent Voice MCP Server.

Lookup order: $AGENT_VOICE_CONFIG → ~/.local/share/agent-voice-mcp/config.json → defaults
Environment variables override individual config values.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from shared import get_logger

logger = get_logger("config")

DEFAULT_CONFIG_PATH = Path.home() / ".local" / "share" / "agent-voice-mcp" / "config.json"
DEFAULT_MODEL_DIR = Path.home() / ".local" / "share" / "agent-voice-mcp" / "models"


@dataclass
class TTSConfig:
    model_path: str = str(DEFAULT_MODEL_DIR / "kokoro-v1.0.onnx")
    voices_path: str = str(DEFAULT_MODEL_DIR / "voices-v1.0.bin")
    default_voice: str = "am_eric"
    default_speed: float = 1.0
    audio_player: str = "mpv"


@dataclass
class STTConfig:
    model_size: str = "base"
    language: str = "en"
    silence_threshold: float = 1.5
    max_listen_timeout: float = 15


@dataclass
class AppConfig:
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    main_agent: str = "Eric"
    voice_registry: dict[str, str] = field(default_factory=dict)
    log_level: str = "info"
    log_file: bool = False
    http_port: int = 7865


def get_config_path() -> Path:
    """Return the config file path, respecting $AGENT_VOICE_CONFIG."""
    env_path = os.environ.get("AGENT_VOICE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """Load configuration from JSON file with env var overrides.

    Lookup order:
    1. Explicit config_path argument
    2. $AGENT_VOICE_CONFIG environment variable
    3. ~/.local/share/agent-voice-mcp/config.json
    4. Built-in defaults
    """
    path = config_path or get_config_path()
    config = AppConfig()

    # Load from file if it exists
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)

            # TTS config
            if "tts" in data:
                tts = data["tts"]
                if "model_path" in tts:
                    config.tts.model_path = str(Path(tts["model_path"]).expanduser())
                if "voices_path" in tts:
                    config.tts.voices_path = str(Path(tts["voices_path"]).expanduser())
                if "default_voice" in tts:
                    config.tts.default_voice = tts["default_voice"]
                if "default_speed" in tts:
                    config.tts.default_speed = float(tts["default_speed"])
                if "audio_player" in tts:
                    config.tts.audio_player = tts["audio_player"]

            # STT config
            if "stt" in data:
                stt = data["stt"]
                if "model_size" in stt:
                    config.stt.model_size = stt["model_size"]
                if "language" in stt:
                    config.stt.language = stt["language"]
                if "silence_threshold" in stt:
                    config.stt.silence_threshold = float(stt["silence_threshold"])
                if "max_listen_timeout" in stt:
                    config.stt.max_listen_timeout = float(stt["max_listen_timeout"])

            # Top-level config
            if "main_agent" in data:
                config.main_agent = data["main_agent"]
            if "voice_registry" in data:
                config.voice_registry = dict(data["voice_registry"])
            if "log_level" in data:
                config.log_level = data["log_level"]
            if "log_file" in data:
                config.log_file = bool(data["log_file"])
            if "http_port" in data:
                config.http_port = int(data["http_port"])

            logger.debug(f"Loaded config from {path}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Error reading config from {path}: {e}. Using defaults.")

    # Environment variable overrides
    if env_model := os.environ.get("KOKORO_MODEL"):
        config.tts.model_path = str(Path(env_model).expanduser())
    if env_voices := os.environ.get("KOKORO_VOICES"):
        config.tts.voices_path = str(Path(env_voices).expanduser())
    if env_whisper := os.environ.get("WHISPER_MODEL"):
        config.stt.model_size = env_whisper
    if env_player := os.environ.get("VOICE_PLAYER"):
        config.tts.audio_player = env_player
    if env_default := os.environ.get("VOICE_DEFAULT"):
        config.tts.default_voice = env_default
    if env_port := os.environ.get("VOICE_HTTP_PORT"):
        config.http_port = int(env_port)

    return config


def save_config(config: AppConfig, config_path: Optional[Path] = None) -> None:
    """Save configuration to JSON file."""
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "tts": {
            "model_path": config.tts.model_path,
            "voices_path": config.tts.voices_path,
            "default_voice": config.tts.default_voice,
            "default_speed": config.tts.default_speed,
            "audio_player": config.tts.audio_player,
        },
        "stt": {
            "model_size": config.stt.model_size,
            "language": config.stt.language,
            "silence_threshold": config.stt.silence_threshold,
            "max_listen_timeout": config.stt.max_listen_timeout,
        },
        "main_agent": config.main_agent,
        "voice_registry": config.voice_registry,
        "log_level": config.log_level,
        "log_file": config.log_file,
        "http_port": config.http_port,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    logger.debug(f"Saved config to {path}")
