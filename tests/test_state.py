"""Tests for shared state (global mute + volume) and server integration."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server_state as ss
from shared import SynthesisResult, PlaybackResult, TranscriptionResult


@pytest.fixture(autouse=True)
def temp_state(tmp_path, monkeypatch):
    """Point the shared state at a temp dir for every test."""
    monkeypatch.setattr(ss, "_data_dir", lambda: tmp_path)
    yield


# ─── server_state unit tests ──────────────────────────────────────────────────


class TestServerState:
    def test_defaults_when_missing(self):
        state = ss.read_state()
        assert state == {"mute_all": False, "mute_subagents": False, "volume": 1.0}

    def test_update_and_read_round_trip(self):
        ss.update_state(mute_all=True, volume=0.4)
        state = ss.read_state()
        assert state["mute_all"] is True
        assert state["mute_subagents"] is False  # untouched field preserved
        assert state["volume"] == 0.4

    def test_update_read_modify_write(self):
        ss.update_state(mute_all=True)
        ss.update_state(volume=0.25)  # must not clobber mute_all
        state = ss.read_state()
        assert state["mute_all"] is True
        assert state["volume"] == 0.25

    def test_corrupt_file_falls_back_to_defaults(self):
        ss._data_dir().mkdir(parents=True, exist_ok=True)
        (ss._data_dir() / "state.json").write_text("{not json")
        assert ss.read_state() == {"mute_all": False, "mute_subagents": False, "volume": 1.0}

    def test_volume_clamped(self):
        state = ss.update_state(volume=5.0)  # 1-100 treated as percent → 5%
        assert state["volume"] == 0.05
        state = ss.update_state(volume=150.0)  # beyond percent range → clamped to 1.0
        assert state["volume"] == 1.0
        state = ss.update_state(volume=-2.0)  # negative → clamped to 0.0
        assert state["volume"] == 0.0

    def test_clamp_volume_accepts_percent(self):
        assert ss.clamp_volume(50) == 0.5   # percent
        assert ss.clamp_volume(0.5) == 0.5  # fraction
        assert ss.clamp_volume(100) == 1.0
        assert ss.clamp_volume("x") == 1.0
        assert ss.clamp_volume(True) == 1.0  # bool → not a number → default


# ─── AudioPlayer gain ─────────────────────────────────────────────────────────


class TestApplyGain:
    def test_gain_half(self):
        from tts.audio_player import apply_gain
        samples = np.array([0.5, -0.8, 0.25], dtype=np.float32)
        out = apply_gain(samples, 0.5)
        assert np.allclose(out, [0.25, -0.4, 0.125], atol=1e-6)

    def test_gain_clips(self):
        from tts.audio_player import apply_gain
        samples = np.array([0.9, -0.9], dtype=np.float32)
        out = apply_gain(samples, 1.0)  # gain 1.0 is a no-op
        assert np.allclose(out, samples)
        # Clamping happens for gains applied on top of hot samples
        hot = np.array([1.5, -1.5], dtype=np.float32)
        out = apply_gain(hot, 0.5)
        assert np.allclose(out, [0.75, -0.75])

    def test_gain_zero_silences(self):
        from tts.audio_player import apply_gain
        samples = np.ones(8, dtype=np.float32)
        out = apply_gain(samples, 0.0)
        assert np.allclose(out, 0.0)

    def test_gain_unity_returns_same_object(self):
        from tts.audio_player import apply_gain
        samples = np.ones(8, dtype=np.float32)
        assert apply_gain(samples, 1.0) is samples


# ─── Server integration ───────────────────────────────────────────────────────


def _setup(tmp_path, tts=True, stt=False, t3_thread_id=None):
    import server
    from voice_registry import VoiceRegistry
    from voice_floor import VoiceFloor

    if tts:
        engine = MagicMock()
        engine.is_loaded.return_value = True
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=10.0)
        queue = MagicMock()
        queue.depth = 0
        queue.stop.return_value = False

        async def mock_speak(text, voice_id, speed=1.0, block=True):
            return SynthesisResult(
                success=None, voice=voice_id  # placeholder, replaced below
            ).__class__(  # build a real SpeakResult
                success=True, voice=voice_id, duration_ms=10.0, synthesis_ms=5.0
            )

        from shared import SpeakResult

        async def mock_speak2(text, voice_id, speed=1.0, block=True):
            return SpeakResult(success=True, voice=voice_id, duration_ms=10.0, synthesis_ms=5.0)

        queue.speak = mock_speak2
        server._tts_engine = engine
        server._audio_player = player
        server._speech_queue = queue

    if stt:
        stt_engine = MagicMock()
        stt_engine.is_loaded.return_value = True
        stt_engine.transcribe.return_value = TranscriptionResult(
            text="hi", confidence=0.9, transcription_ms=1.0, language="en",
        )
        mic = MagicMock()
        mic.is_recording = False
        server._stt_engine = stt_engine
        server._vad = MagicMock()
        server._mic_capture = mic

    server._registry = VoiceRegistry(default_voice="am_eric")
    server._muted = False
    server._session_volume = None
    server._wake_listener = None
    server._listen_active = False
    server._listen_cancel_event = None
    session = {"name": "Eric", "voice": "am_eric", "pid": os.getpid()}
    if t3_thread_id is not None:
        session["t3_thread_id"] = t3_thread_id
    server._session_info = session
    server._voice_floor = VoiceFloor(
        lock_path=str(tmp_path / "f.lock"),
        info_path=str(tmp_path / "f.json"),
    )
    server._play_ready_sound = lambda: None

    server._config = MagicMock()
    server._config.coordination.enabled = True
    server._config.coordination.floor_wait = 0.2
    server._config.engine.enabled = False
    server._config.tts.duck_media = False
    server._config.stt.nudge_on_timeout = False
    server._config.last_voice_name = None
    server._config.main_agent = "Eric"
    return server


class TestEffectiveMute:
    @pytest.mark.asyncio
    async def test_mute_all_mutes_speak(self, tmp_path):
        server = _setup(tmp_path)
        ss.update_state(mute_all=True)

        result = await server.speak("Eric", "Hello", block=True)
        assert result["success"] is True
        assert result.get("muted") is True

        # And listening is blocked too — meeting mode means mic off as well
        server._stt_engine = MagicMock(); server._mic_capture = MagicMock()
        listen = await server.listen(timeout=1)
        assert listen["success"] is False
        assert listen["error"] == "muted"

    @pytest.mark.asyncio
    async def test_mute_all_applies_to_new_sessions(self, tmp_path):
        """The whole point: a session spawned AFTER the flag still mutes."""
        ss.update_state(mute_all=True)
        server = _setup(tmp_path)  # "spawned" after the flag was set
        assert server._effective_mute() is True

        ss.update_state(mute_all=False)
        assert server._effective_mute() is False

    @pytest.mark.asyncio
    async def test_mute_subagents_only_mutes_subagents(self, tmp_path):
        ss.update_state(mute_subagents=True)
        server = _setup(tmp_path)  # module singleton: swap session identity inline

        # Main thread (plain UUID): not a subagent, not muted
        server._session_info["t3_thread_id"] = "86a2747d-d68c-49e8-8f4f-a3ea4a2000e9"
        assert server._session_is_subagent() is False
        assert server._effective_mute() is False

        # Agent-spawned child thread (prefixed id): subagent → muted
        server._session_info["t3_thread_id"] = "thread:delegated-task:xyz"
        assert server._session_is_subagent() is True
        assert server._effective_mute() is True

        # Unbound sessions (non-T3) are treated as main threads
        del server._session_info["t3_thread_id"]
        assert server._effective_mute() is False

    @pytest.mark.asyncio
    async def test_session_mute_still_works_alone(self, tmp_path):
        server = _setup(tmp_path)
        server._muted = True
        assert server._effective_mute() is True
        server._muted = False
        assert server._effective_mute() is False


class TestVolume:
    @pytest.mark.asyncio
    async def test_set_volume_session_scope(self, tmp_path):
        server = _setup(tmp_path)
        result = await server.set_volume(0.3)
        assert result["success"] is True
        assert result["scope"] == "session"
        assert server._session_volume == 0.3
        assert server._playback_gain() == 0.3

    @pytest.mark.asyncio
    async def test_set_volume_all_scope(self, tmp_path):
        server = _setup(tmp_path)
        result = await server.set_volume(40, scope="all")  # percent
        assert result["success"] is True
        assert ss.read_state()["volume"] == 0.4

        # Without an override, the global volume applies...
        assert server._playback_gain() == 0.4
        # ...but a session override wins
        await server.set_volume(0.1)
        assert server._playback_gain() == 0.1
        # ...and clearing the override falls back to global
        server._session_volume = None
        assert server._playback_gain() == 0.4

    @pytest.mark.asyncio
    async def test_set_volume_rejects_bad_values(self, tmp_path):
        server = _setup(tmp_path)
        result = await server.set_volume("loud")
        assert result["success"] is False
        assert result["error"] == "invalid_volume"
        result = await server.set_volume(0.5, scope="everyone")
        assert result["success"] is False
        assert result["error"] == "invalid_scope"

    @pytest.mark.asyncio
    async def test_speech_queue_passes_gain_to_player(self, tmp_path):
        """The queue asks the volume provider and hands the gain to play()."""
        from tts.speech_queue import SpeechQueue

        engine = MagicMock()
        engine.synthesize.return_value = SynthesisResult(
            samples=np.zeros(2400, dtype=np.float32), sample_rate=24000,
            duration_ms=100.0, synthesis_ms=10.0,
        )
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=100.0)

        queue = SpeechQueue(engine, player, volume_provider=lambda: 0.25)
        result = await queue._speak_blocking("Hello", "am_eric", 1.0)

        assert result.success is True
        assert player.play.call_args.args[2] == 0.25  # gain passed through

    @pytest.mark.asyncio
    async def test_status_reports_mute_and_volume(self, tmp_path):
        server = _setup(tmp_path, t3_thread_id="thread:provider:codex:abc")
        ss.update_state(mute_subagents=True, volume=0.6)

        result = await server.status()
        assert result["mute"]["is_subagent"] is True
        assert result["mute"]["subagents"] is True
        assert result["mute"]["effective"] is True
        assert result["volume"]["global"] == 0.6
        assert result["volume"]["effective"] == 0.6