"""Tests for shared state (global mute + volume) and server integration."""

import json
import os
import sys
import time
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
        assert state == {"mute_all": False, "mute_subagents": False,
                         "mute_mic": False, "volume": 1.0}

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
        assert ss.read_state() == {"mute_all": False, "mute_subagents": False,
                                   "mute_mic": False, "volume": 1.0}

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


class FakeProc:
    """Popen-like stub for the live-control loop."""
    def __init__(self, exits_after_polls: int = 9999):
        self.poll_count = 0
        self.exits_after = exits_after_polls
        self.killed = False
        self.returncode = None

    def poll(self):
        self.poll_count += 1
        if self.poll_count >= self.exits_after:
            self.returncode = 0
            return 0
        return None

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0


class TestLivePlaybackControl:
    """_wait_with_controls: mute kills mid-playback, volume pushes over IPC."""

    def _player(self, mute_provider=None, volume_provider=None):
        from tts.audio_player import AudioPlayer
        player = AudioPlayer.__new__(AudioPlayer)  # skip __init__ (player detection)
        player._player_command = "mpv"
        player._audio_output_device = None
        player._mute_provider = mute_provider
        player._volume_provider = volume_provider
        player._process = None
        player._ipc_seq = 0
        return player

    def test_normal_exit(self):
        player = self._player()
        proc = FakeProc(exits_after_polls=3)
        player._process = proc
        assert player._wait_with_controls(None) is False
        assert proc.killed is False

    def test_mute_engages_midplayback_kills(self):
        player = self._player(mute_provider=lambda: True)
        proc = FakeProc()
        player._process = proc
        assert player._wait_with_controls(None) is True
        assert proc.killed is True  # silenced within one poll (~150ms)

    def test_mute_flips_on_after_a_few_polls(self):
        polls = {"n": 0}
        def mute():
            polls["n"] += 1
            return polls["n"] > 3  # user hits Mute All mid-utterance
        player = self._player(mute_provider=mute)
        proc = FakeProc()
        player._process = proc
        assert player._wait_with_controls(None) is True
        assert proc.killed is True

    def test_volume_change_detected_and_sent(self):
        """Integration through a real socketpair: mpv receives 'set volume 25'."""
        import socket as socket_mod
        volumes = [0.5, 0.5, 0.25, 0.25, 0.25]
        player = self._player(volume_provider=lambda: volumes.pop(0) if volumes else 0.25)
        proc = FakeProc(exits_after_polls=5)
        player._process = proc

        listener, writer = socket_mod.socketpair()
        received = []
        import threading
        def reader():
            while True:
                data = listener.recv(1024)
                if not data:
                    break
                received.append(data.decode())
        t = threading.Thread(target=reader, daemon=True)
        t.start()

        assert player._wait_with_controls(writer) is False
        writer.close()
        listener.close()
        t.join(timeout=1)
        assert any("set volume 25" in r for r in received)

    def test_mute_errors_in_provider_are_ignored(self):
        def broken():
            raise RuntimeError("state file gone")
        player = self._player(mute_provider=broken)
        proc = FakeProc(exits_after_polls=3)
        player._process = proc
        assert player._wait_with_controls(None) is False  # playback completes
        assert proc.killed is False

    def test_lazy_socket_path_connects_when_it_appears(self):
        """Volume pushes begin as soon as mpv finally creates its IPC socket."""
        import socket as socket_mod
        import threading
        sock_path = "/tmp/voicesmith-test-lazy-ipc.sock"
        try:
            os.unlink(sock_path)
        except OSError:
            pass

        player = self._player(volume_provider=lambda: 0.4)
        proc = FakeProc(exits_after_polls=25)  # ~2.5s of playback
        player._process = proc

        listener = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        received = []
        def late_bind():
            time.sleep(0.3)  # mpv "creates" the socket late (USB device init)
            listener.bind(sock_path)
            listener.listen(1)
            conn, _ = listener.accept()
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                received.append(data.decode())
            conn.close()
        t = threading.Thread(target=late_bind, daemon=True)
        t.start()

        # Socket "appears" 0.5s in — the loop must pick it up after failures
        assert player._wait_with_controls(ipc=sock_path) is False
        listener.close()
        t.join(timeout=2)
        assert any("set volume 40" in r for r in received), received

    def test_lazy_socket_path_never_connecting_is_harmless(self):
        player = self._player(volume_provider=lambda: 0.4)
        proc = FakeProc(exits_after_polls=5)
        player._process = proc
        # Path that never exists — loop retries quietly until playback ends
        assert player._wait_with_controls(ipc="/tmp/definitely-not-here.sock") is False
        assert proc.killed is False


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
    async def test_mute_mic_blocks_listen_but_not_speak(self, tmp_path):
        """Late-night mode: agents stay audible, mic never opens."""
        server = _setup(tmp_path)
        server._stt_engine = MagicMock()
        server._mic_capture = MagicMock()

        ss.update_state(mute_mic=True)

        # Speech is NOT muted
        assert server._effective_mute() is False
        result = await server.speak("Eric", "Still audible", block=True)
        assert result["success"] is True
        assert result.get("muted") is None  # actually spoke

        # The mic IS closed, with a clear fall-back-to-text message
        assert server._effective_mic_mute() is True
        listen = await server.listen(timeout=1)
        assert listen["success"] is False
        assert listen["error"] == "muted"
        assert "text" in listen["message"]

    @pytest.mark.asyncio
    async def test_mute_mic_in_speak_then_listen(self, tmp_path):
        """speak_then_listen: question plays aloud, listen falls back to text."""
        server = _setup(tmp_path, tts=True, stt=True)
        ss.update_state(mute_mic=True)

        result = await server.speak_then_listen("Eric", "Should I merge?")
        assert result["speak"]["success"] is True
        assert result["speak"].get("muted") is None      # audible
        assert result["listen"]["success"] is False
        assert result["listen"]["error"] == "muted"      # mic closed

    @pytest.mark.asyncio
    async def test_mute_all_still_blocks_both(self, tmp_path):
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
    async def test_speech_queue_stops_remaining_chunks_when_muted(self):
        """A muted mid-playback chunk aborts the utterance (stopped=True)."""
        from unittest.mock import MagicMock
        from tts.speech_queue import SpeechQueue
        from shared import SynthesisResult, PlaybackResult
        import numpy as np

        engine = MagicMock()
        engine.synthesize.return_value = SynthesisResult(
            samples=np.zeros(100, dtype=np.float32), sample_rate=24000,
            duration_ms=50.0, synthesis_ms=5.0,
        )
        player = MagicMock()
        # First chunk: killed mid-playback (mute engaged)
        player.play.return_value = PlaybackResult(success=True, duration_ms=50.0, stopped=True)

        # Long text → several chunks (chunk_text splits above 500 chars)
        long_text = ("The quick brown fox jumps over the lazy dog. " * 20).strip()
        assert len(long_text) > 500

        queue = SpeechQueue(engine, player)
        result = await queue._speak_blocking(long_text, "am_eric", 1.0)

        assert result.success is True
        assert result.stopped is True
        # Only the first chunk was synthesized — the rest were skipped
        assert engine.synthesize.call_count == 1
        assert player.play.call_count == 1
        args = player.play.call_args
        assert len(args.args) == 2  # samples, sample_rate — no gain plumbing

    @pytest.mark.asyncio
    async def test_speech_queue_normal_multi_chunk(self):
        from unittest.mock import MagicMock
        from tts.speech_queue import SpeechQueue
        from shared import SynthesisResult, PlaybackResult
        import numpy as np

        engine = MagicMock()
        engine.synthesize.return_value = SynthesisResult(
            samples=np.zeros(100, dtype=np.float32), sample_rate=24000,
            duration_ms=50.0, synthesis_ms=5.0,
        )
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=50.0)

        long_text = ("The quick brown fox jumps over the lazy dog. " * 20).strip()
        queue = SpeechQueue(engine, player)
        result = await queue._speak_blocking(long_text, "am_eric", 1.0)
        assert result.success is True
        assert result.stopped is False
        assert engine.synthesize.call_count >= 2  # actually chunked
        assert player.play.call_count == engine.synthesize.call_count

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