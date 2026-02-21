"""Tests for voice_registry.py."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from voice_registry import VoiceRegistry
from shared import ALL_VOICE_IDS, VOICE_NAME_MAP


class TestNameMatching:
    """Test name-based voice auto-assignment."""

    def test_eric_maps_to_am_eric(self):
        reg = VoiceRegistry()
        voice, auto = reg.get_voice("Eric")
        assert voice == "am_eric"
        assert auto is True

    def test_nova_maps_to_af_nova(self):
        reg = VoiceRegistry()
        voice, auto = reg.get_voice("Nova")
        assert voice == "af_nova"
        assert auto is True

    def test_case_insensitivity(self):
        reg = VoiceRegistry()
        voice, auto = reg.get_voice("eric")
        assert voice == "am_eric"
        assert auto is True

    def test_second_lookup_is_not_auto_assigned(self):
        reg = VoiceRegistry()
        reg.get_voice("Eric")
        voice, auto = reg.get_voice("Eric")
        assert voice == "am_eric"
        assert auto is False

    def test_name_match_avoids_already_assigned_voice(self):
        """If the name-matched voice is already taken, fall through to hash."""
        reg = VoiceRegistry(preloaded_registry={"SomeAgent": "am_eric"})
        voice, auto = reg.get_voice("Eric")
        # am_eric is taken by SomeAgent, so Eric should get a different voice
        assert voice != "am_eric"
        assert auto is True


class TestHashBasedAssignment:
    """Test hash-based voice assignment for unknown names."""

    def test_unknown_name_gets_a_voice(self):
        reg = VoiceRegistry()
        voice, auto = reg.get_voice("UnknownAgent42")
        assert voice in ALL_VOICE_IDS
        assert auto is True

    def test_deterministic_assignment(self):
        """Same name always gets the same voice."""
        reg1 = VoiceRegistry()
        voice1, _ = reg1.get_voice("AgentX")

        reg2 = VoiceRegistry()
        voice2, _ = reg2.get_voice("AgentX")

        assert voice1 == voice2

    def test_different_names_get_different_voices(self):
        """Different names should generally get different voices."""
        reg = VoiceRegistry()
        voice1, _ = reg.get_voice("AgentAlpha")
        voice2, _ = reg.get_voice("AgentBeta")
        # Not guaranteed to be different due to hash collisions,
        # but the pool is large enough that this should hold for most pairs
        assert voice1 != voice2


class TestPoolExhaustion:
    """Test behavior when all voices are assigned."""

    def test_exhaust_pool_then_reuse(self):
        reg = VoiceRegistry()
        total_voices = len(ALL_VOICE_IDS)

        # Assign all voices
        assigned = set()
        for i in range(total_voices):
            voice, auto = reg.get_voice(f"Agent_{i:04d}")
            assigned.add(voice)
            assert auto is True

        # Pool should be empty
        assert len(reg.get_available_pool()) == 0

        # Next agent should still get a voice (reused from full pool)
        voice, auto = reg.get_voice("Agent_overflow")
        assert voice in ALL_VOICE_IDS
        assert auto is True

    def test_pool_shrinks_as_voices_assigned(self):
        reg = VoiceRegistry()
        initial_pool = len(reg.get_available_pool())
        assert initial_pool == 54

        reg.get_voice("Eric")
        assert len(reg.get_available_pool()) == initial_pool - 1

        reg.get_voice("Nova")
        assert len(reg.get_available_pool()) == initial_pool - 2


class TestSetVoice:
    """Test explicit voice assignment."""

    def test_set_valid_voice(self):
        reg = VoiceRegistry()
        result = reg.set_voice("MyAgent", "am_onyx")
        assert result is True
        voice, auto = reg.get_voice("MyAgent")
        assert voice == "am_onyx"
        assert auto is False

    def test_set_invalid_voice(self):
        reg = VoiceRegistry()
        result = reg.set_voice("MyAgent", "am_nonexistent")
        assert result is False
        # Should not be registered
        assert reg.size == 0

    def test_override_existing(self):
        reg = VoiceRegistry()
        reg.get_voice("Eric")  # auto-assigns am_eric
        result = reg.set_voice("Eric", "af_nova")
        assert result is True
        voice, auto = reg.get_voice("Eric")
        assert voice == "af_nova"
        assert auto is False


class TestPersistence:
    """Test save/load of registry."""

    def test_save_and_load(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        # Save
        reg1 = VoiceRegistry(config_path=config_path)
        reg1.get_voice("Eric")
        reg1.get_voice("Nova")
        reg1.set_voice("Custom", "am_onyx")
        reg1.save()

        # Load into new registry
        reg2 = VoiceRegistry(config_path=config_path)
        assert reg2.size == 3
        voice, auto = reg2.get_voice("Eric")
        assert voice == "am_eric"
        assert auto is False  # loaded, not auto-assigned

    def test_save_preserves_other_config(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"log_level": "debug", "main_agent": "Eric"}))

        reg = VoiceRegistry(config_path=config_path)
        reg.set_voice("Test", "am_adam")
        reg.save()

        with open(config_path) as f:
            data = json.load(f)

        assert data["log_level"] == "debug"
        assert data["main_agent"] == "Eric"
        assert data["voice_registry"]["Test"] == "am_adam"

    def test_load_from_constructor(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "voice_registry": {"Alice": "bf_alice", "George": "bm_george"}
        }))

        reg = VoiceRegistry(config_path=config_path)
        assert reg.size == 2
        voice, auto = reg.get_voice("Alice")
        assert voice == "bf_alice"
        assert auto is False


class TestGetRegistry:
    """Test get_registry returns a copy."""

    def test_returns_copy(self):
        reg = VoiceRegistry()
        reg.get_voice("Eric")
        copy = reg.get_registry()
        copy["Eric"] = "af_nova"  # modify the copy
        voice, _ = reg.get_voice("Eric")
        assert voice == "am_eric"  # original unchanged

    def test_empty_registry(self):
        reg = VoiceRegistry()
        assert reg.get_registry() == {}
        assert reg.size == 0


class TestPreloadedRegistry:
    """Test initialization with preloaded registry."""

    def test_preloaded(self):
        reg = VoiceRegistry(preloaded_registry={"Agent1": "am_eric", "Agent2": "af_nova"})
        assert reg.size == 2
        voice, auto = reg.get_voice("Agent1")
        assert voice == "am_eric"
        assert auto is False

    def test_preloaded_is_copied(self):
        original = {"Agent1": "am_eric"}
        reg = VoiceRegistry(preloaded_registry=original)
        original["Agent2"] = "af_nova"
        assert reg.size == 1  # not affected by external mutation
