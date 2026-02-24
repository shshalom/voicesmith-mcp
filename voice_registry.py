"""
Voice registry for VoiceSmith MCP Server.

Maps agent names to Kokoro voice IDs with auto-discovery:
1. Name matching (e.g., "Eric" -> am_eric)
2. Hash-based assignment from unassigned pool
3. Pool exhaustion fallback (reuses voices)
"""

import json
from pathlib import Path
from typing import Optional

from shared import ALL_VOICE_IDS, VOICE_NAME_MAP, get_logger

logger = get_logger("voice-registry")


class VoiceRegistry:
    """Manages agent name -> voice ID mappings with auto-discovery."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        preloaded_registry: Optional[dict[str, str]] = None,
        default_voice: str = "am_eric",
    ):
        self._registry: dict[str, str] = {}
        self._config_path = config_path
        self._default_voice = default_voice

        if config_path and config_path.exists():
            self.load(config_path)
        elif preloaded_registry is not None:
            self._registry = dict(preloaded_registry)

    def get_voice(self, name: str) -> tuple[str, bool]:
        """Return (voice_id, auto_assigned) for the given agent name.

        Lookup order:
        1. Existing registry entry
        2. Name matching against VOICE_NAME_MAP
        3. Hash-based assignment from unassigned pool
        4. Hash-based fallback from full pool (if pool exhausted)
        """
        # 1. Already registered
        if name in self._registry:
            return (self._registry[name], False)

        # 2. Name matching (case-insensitive)
        lower_name = name.lower()
        if lower_name in VOICE_NAME_MAP:
            candidate = VOICE_NAME_MAP[lower_name]
            assigned_voices = set(self._registry.values())
            if candidate not in assigned_voices:
                self._registry[name] = candidate
                logger.info(f"Auto-assigned voice '{candidate}' to '{name}' (name match)")
                return (candidate, True)

        # 3. Hash-based assignment from unassigned pool
        pool = self.get_available_pool()
        if pool:
            index = hash(name) % len(pool)
            voice_id = pool[index]
            self._registry[name] = voice_id
            logger.info(f"Auto-assigned voice '{voice_id}' to '{name}' (hash from pool)")
            return (voice_id, True)

        # 4. Pool exhausted — pick from full set
        logger.warning("All voices assigned, reusing voices.")
        all_sorted = sorted(ALL_VOICE_IDS)
        index = hash(name) % len(all_sorted)
        voice_id = all_sorted[index]
        self._registry[name] = voice_id
        logger.info(f"Auto-assigned voice '{voice_id}' to '{name}' (hash from full pool, reuse)")
        return (voice_id, True)

    def set_voice(self, name: str, voice_id: str) -> bool:
        """Assign a specific voice to an agent name.

        Returns True if the voice_id is valid, False otherwise.
        """
        if voice_id not in ALL_VOICE_IDS:
            logger.warning(f"Invalid voice ID '{voice_id}' for '{name}'")
            return False
        self._registry[name] = voice_id
        logger.info(f"Set voice '{voice_id}' for '{name}'")
        return True

    def get_registry(self) -> dict[str, str]:
        """Return a copy of the current registry."""
        return dict(self._registry)

    def get_available_pool(self) -> list[str]:
        """Return sorted list of voice IDs not currently assigned."""
        assigned = set(self._registry.values())
        return sorted(ALL_VOICE_IDS - assigned)

    def save(self, config_path: Optional[Path] = None) -> None:
        """Save registry to config JSON file.

        Reads existing config, updates the voice_registry key, writes back.
        """
        path = config_path or self._config_path
        if path is None:
            logger.warning("No config path specified, cannot save registry")
            return

        data = {}
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Error reading config for save: {e}")

        data["voice_registry"] = dict(self._registry)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        logger.debug(f"Saved registry ({self.size} entries) to {path}")

    def load(self, config_path: Optional[Path] = None) -> None:
        """Load registry from config JSON file."""
        path = config_path or self._config_path
        if path is None:
            logger.warning("No config path specified, cannot load registry")
            return

        if not path.exists():
            logger.debug(f"Config file not found at {path}, starting with empty registry")
            return

        try:
            with open(path) as f:
                data = json.load(f)
            if "voice_registry" in data:
                self._registry = dict(data["voice_registry"])
                logger.debug(f"Loaded registry ({self.size} entries) from {path}")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error loading registry from {path}: {e}")

    @property
    def size(self) -> int:
        """Return number of entries in the registry."""
        return len(self._registry)
