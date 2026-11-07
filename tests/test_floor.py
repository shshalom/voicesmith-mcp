"""Tests for the cross-process voice floor (speaker + mic arbitration)."""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from voice_floor import VoiceFloor, FloorBusyError


def make_floor(tmp_path, name: str = "f") -> VoiceFloor:
    return VoiceFloor(
        lock_path=str(tmp_path / f"{name}.lock"),
        info_path=str(tmp_path / f"{name}.json"),
    )


@pytest.fixture(autouse=True)
def _restore_server_globals():
    """Snapshot/restore server module globals around every test here.

    _setup_floor_server mutates module-level state (_session_info,
    _voice_floor, ...) that test_server.py's helpers don't reset; leaking
    it changes their behavior.
    """
    import server
    saved = (
        server._session_info,
        server._voice_floor,
        server._play_ready_sound,
        server._config,
    )
    yield
    (
        server._session_info,
        server._voice_floor,
        server._play_ready_sound,
        server._config,
    ) = saved


# ─── VoiceFloor unit tests ────────────────────────────────────────────────────


class TestVoiceFloor:
    def test_acquire_and_release(self, tmp_path):
        floor = make_floor(tmp_path)
        floor.acquire_sync("speak", "Eric")
        assert floor.holder_info() is not None
        assert floor.holder_info()["name"] == "Eric"
        assert floor.holder_info()["purpose"] == "speak"
        floor.release()
        assert floor.holder_info() is None

    def test_second_instance_excluded(self, tmp_path):
        a = make_floor(tmp_path)
        b = make_floor(tmp_path)
        a.acquire_sync("listen", "Nova")

        with pytest.raises(FloorBusyError) as exc_info:
            b.acquire_sync("speak", "Eric", wait_timeout=0.2)

        assert exc_info.value.holder["name"] == "Nova"
        assert exc_info.value.holder["purpose"] == "listen"
        a.release()
        b.acquire_sync("speak", "Eric", wait_timeout=1.0)  # now succeeds
        b.release()

    def test_reentrant_sync_same_thread(self, tmp_path):
        a = make_floor(tmp_path)
        a.acquire_sync("speak_then_listen", "Eric")
        a.acquire_sync("speak", "Eric")  # nested — no-op
        a.release()
        assert a.holder_info() is not None  # still held at depth 1
        a.release()
        assert a.holder_info() is None

    def test_stale_sidecar_detected_as_free(self, tmp_path):
        floor = make_floor(tmp_path)
        # Simulate a crashed holder: sidecar exists but the lock is free.
        Path(floor._info_path).write_text('{"pid": 999, "name": "Ghost"}')
        assert floor.holder_info() is None
        assert not Path(floor._info_path).exists()  # stale sidecar cleaned up

    def test_acquire_sync_from_plain_thread(self, tmp_path):
        floor = make_floor(tmp_path)
        held = threading.Event()
        error: list = []

        def worker():
            try:
                floor.acquire_sync("speak", "Thread", wait_timeout=2.0)
                held.set()
            except Exception as e:  # pragma: no cover
                error.append(e)

        t = threading.Thread(target=worker)
        t.start()
        assert held.wait(timeout=3.0), f"thread acquire failed: {error}"
        assert floor.holder_info()["purpose"] == "speak"
        floor.release()  # release from main thread is wrong owner — should warn, not crash
        # The thread still holds at depth 1 (release by non-owner is a no-op).
        assert floor.holder_info() is not None
        # Release from the owning thread context: simulate by keying on it.
        t.join(timeout=1)


# ─── Async acquire tests ───────────────────────────────────────────────────────


class TestAsyncAcquire:
    @pytest.mark.asyncio
    async def test_reentrant_same_task(self, tmp_path):
        floor = make_floor(tmp_path)
        await floor.acquire("speak_then_listen", "Eric")
        await floor.acquire("speak", "Eric")  # nested from same task — no-op
        floor.release()
        assert floor.holder_info() is not None
        floor.release()
        assert floor.holder_info() is None

    @pytest.mark.asyncio
    async def test_wait_until_released(self, tmp_path):
        a = make_floor(tmp_path)
        b = make_floor(tmp_path)
        a.acquire_sync("listen", "Nova")

        async def waiter():
            await b.acquire("speak", "Eric", wait_timeout=3.0)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.15)
        assert not task.done()  # still waiting on the floor
        a.release()
        await asyncio.wait_for(task, timeout=2.0)
        assert b.holder_info()["name"] == "Eric"
        b.release()

    @pytest.mark.asyncio
    async def test_timeout_raises_floor_busy(self, tmp_path):
        a = make_floor(tmp_path)
        b = make_floor(tmp_path)
        a.acquire_sync("speak", "Nova")

        with pytest.raises(FloorBusyError) as exc_info:
            await b.acquire("listen", "Eric", wait_timeout=0.3)

        assert exc_info.value.holder["name"] == "Nova"
        a.release()

    @pytest.mark.asyncio
    async def test_cancelled_acquire_leaks_nothing(self, tmp_path):
        a = make_floor(tmp_path)
        b = make_floor(tmp_path)
        a.acquire_sync("speak", "Nova")

        task = asyncio.create_task(b.acquire("speak", "Eric", wait_timeout=10.0))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        a.release()
        # The cancelled acquire must not hold the floor: a third party
        # (fresh instance) can take it immediately.
        c = make_floor(tmp_path)
        c.acquire_sync("speak", "Third", wait_timeout=0.5)
        assert c.holder_info()["name"] == "Third"
        c.release()

    @pytest.mark.asyncio
    async def test_concurrent_tasks_serialize(self, tmp_path):
        """Two different tasks in the same process must not share a hold."""
        floor = make_floor(tmp_path)
        order = []

        async def op(tag: str):
            await floor.acquire("speak", tag, wait_timeout=5.0)
            try:
                order.append(f"{tag}-start")
                await asyncio.sleep(0.05)
                order.append(f"{tag}-end")
            finally:
                floor.release()

        await asyncio.gather(op("A"), op("B"))
        # Serialized: one completes fully before the other starts.
        assert order in (["A-start", "A-end", "B-start", "B-end"],
                         ["B-start", "B-end", "A-start", "A-end"])
        assert floor.holder_info() is None


# ─── Sidecar phase refresh + thread identity ──────────────────────────────────


class TestSidecarIdentity:
    @pytest.mark.asyncio
    async def test_reentrant_acquire_refreshes_purpose(self, tmp_path):
        """speak_then_listen must surface its live phase (speak → listen)."""
        floor = make_floor(tmp_path)
        await floor.acquire("speak_then_listen", "Eric")
        acquired_at = floor.holder_info()["acquired_at"]

        await floor.acquire("speak", "Eric")  # inner speak phase
        assert floor.holder_info()["purpose"] == "speak"
        assert floor.holder_info()["acquired_at"] == acquired_at  # age stays honest

        floor.release()  # speak phase done, outer hold continues
        await floor.acquire("listen", "Eric")  # inner listen phase
        assert floor.holder_info()["purpose"] == "listen"

        floor.release()
        floor.release()
        assert floor.holder_info() is None

    @pytest.mark.asyncio
    async def test_default_extra_stamped_into_sidecar(self, tmp_path):
        """Thread identity set once on the floor shows in every hold."""
        floor = make_floor(tmp_path)
        floor.set_default_extra({"thread_id": "t3-abc-123"})

        await floor.acquire("speak", "Eric")
        assert floor.holder_info()["thread_id"] == "t3-abc-123"
        floor.release()
        assert floor.holder_info() is None

    def test_set_session_thread_id(self, tmp_path, monkeypatch):
        import session_registry as sr

        monkeypatch.setattr(sr, "_sessions_path", lambda: tmp_path / "sessions.json")
        (tmp_path / "sessions.json").write_text(json.dumps(
            {"sessions": [{"name": "Eric", "pid": os.getpid(), "voice": "am_eric"}]}
        ))

        updated = sr.set_session_thread_id(os.getpid(), "thread-xyz")
        assert updated is not None
        assert updated["t3_thread_id"] == "thread-xyz"
        assert updated["name"] == "Eric"

        # Persisted for other processes (companions) to read
        data = json.loads((tmp_path / "sessions.json").read_text())
        assert data["sessions"][0]["t3_thread_id"] == "thread-xyz"

    def test_set_session_thread_id_unknown_pid(self, tmp_path, monkeypatch):
        import session_registry as sr

        monkeypatch.setattr(sr, "_sessions_path", lambda: tmp_path / "sessions.json")
        (tmp_path / "sessions.json").write_text(json.dumps({"sessions": []}))

        assert sr.set_session_thread_id(999999, "x") is None


# ─── Live audio phases (set_phase) ────────────────────────────────────────────


class TestPhaseRefinement:
    @pytest.mark.asyncio
    async def test_set_phase_semantics(self, tmp_path):
        """set_phase refines purpose without touching lock or depth."""
        floor = make_floor(tmp_path)

        floor.set_phase("listen")  # not holding — no-op, no crash
        assert floor.holder_info() is None

        await floor.acquire("processing", "Eric")
        acquired_at = floor.holder_info()["acquired_at"]

        time.sleep(0.02)
        floor.set_phase("listen")
        info = floor.holder_info()
        assert info["purpose"] == "listen"
        assert info["name"] == "Eric"            # holder identity preserved
        assert info["acquired_at"] == acquired_at  # held-for age preserved

        floor.release()  # single release frees — set_phase didn't add depth
        assert floor.holder_info() is None

    @pytest.mark.asyncio
    async def test_speech_queue_reports_playback_phases(self):
        """The queue reports synthesizing/speak at real audio boundaries."""
        from unittest.mock import MagicMock
        from tts.speech_queue import SpeechQueue
        from shared import SynthesisResult, PlaybackResult
        import numpy as np

        engine = MagicMock()
        engine.synthesize.return_value = SynthesisResult(
            samples=np.zeros(2400, dtype=np.float32), sample_rate=24000,
            duration_ms=100.0, synthesis_ms=10.0,
        )
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=100.0)

        phases = []
        queue = SpeechQueue(engine, player, phase_reporter=phases.append)

        result = await queue._speak_blocking("Hello there.", "am_eric", 1.0)
        assert result.success is True
        assert phases == ["synthesizing", "speak"]

    @pytest.mark.asyncio
    async def test_speak_then_listen_phase_sequence(self, tmp_path):
        """End-to-end: the sidecar reflects actual playback and mic-open.

        purpose must flip at real audio boundaries — synthesizing during
        TTS, speak during playback, processing during pre-mic setup, listen
        from mic-open (on_ready), transcribing after the mic closes.
        """
        import numpy as np
        from unittest.mock import MagicMock
        from tts.speech_queue import SpeechQueue
        from shared import SynthesisResult, PlaybackResult, TranscriptionResult

        server = _setup_floor_server(tmp_path, tts=True, stt=True)

        class SpyFloor(VoiceFloor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.purposes = []

            def _write_holder(self, purpose, session_name):
                self.purposes.append(purpose)
                super()._write_holder(purpose, session_name)

        spy = SpyFloor(
            lock_path=str(tmp_path / "server.lock"),
            info_path=str(tmp_path / "server.json"),
        )
        server._voice_floor = spy

        # Real SpeechQueue wired to the floor phase bridge
        engine = MagicMock()
        engine.synthesize.return_value = SynthesisResult(
            samples=np.zeros(2400, dtype=np.float32), sample_rate=24000,
            duration_ms=100.0, synthesis_ms=10.0,
        )
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=100.0)
        server._speech_queue = SpeechQueue(
            engine, player, phase_reporter=server._report_floor_phase,
        )

        # Mic mock: on_ready fires (mic-open moment), then returns audio
        async def mock_record(vad=None, timeout=15, silence_threshold=1.5,
                              cancel_event=None, on_ready=None):
            if on_ready:
                on_ready()
            await asyncio.sleep(0.02)
            return np.zeros(16000, dtype=np.float32)

        server._mic_capture.record = mock_record
        server._stt_engine.transcribe.return_value = TranscriptionResult(
            text="go ahead", confidence=0.9, transcription_ms=1.0, language="en",
        )

        result = await server.speak_then_listen("Eric", "What's up?")

        assert result["speak"]["success"] is True
        assert result["listen"]["success"] is True
        assert spy.purposes == [
            "speak_then_listen",  # outer atomic hold
            "synthesizing",      # inner speak acquire (nothing audible yet)
            "synthesizing",      # queue: chunk synthesis starts
            "speak",              # queue: actual playback
            "processing",         # inner listen acquire (pre-mic setup)
            "listen",             # mic-open moment (on_ready)
            "transcribing",       # mic closed, Whisper running
        ]
        assert spy.holder_info() is None  # released at the end


# ─── Server integration tests ─────────────────────────────────────────────────


def _setup_floor_server(tmp_path, tts=True, stt=False):
    """Configure server globals with a floor on temp paths."""
    from unittest.mock import MagicMock
    import server
    from voice_registry import VoiceRegistry
    from shared import PlaybackResult, SpeakResult, TranscriptionResult

    if tts:
        engine = MagicMock()
        engine.is_loaded.return_value = True
        player = MagicMock()
        player.play.return_value = PlaybackResult(success=True, duration_ms=10.0)
        queue = MagicMock()
        queue.depth = 0
        queue.stop.return_value = False

        async def mock_speak(text, voice_id, speed=1.0, block=True):
            return SpeakResult(success=True, voice=voice_id, duration_ms=10.0, synthesis_ms=5.0)

        queue.speak = mock_speak
        server._tts_engine = engine
        server._audio_player = player
        server._speech_queue = queue

    if stt:
        stt_engine = MagicMock()
        stt_engine.is_loaded.return_value = True
        stt_engine.transcribe.return_value = TranscriptionResult(
            text="hello", confidence=0.9, transcription_ms=1.0, language="en",
        )
        mic = MagicMock()
        mic.is_recording = False

        async def mock_record(**kwargs):
            import numpy as np
            await asyncio.sleep(0.05)
            return np.zeros(16000, dtype=np.float32)

        mic.record = mock_record
        server._stt_engine = stt_engine
        server._vad = MagicMock()
        server._mic_capture = mic

    server._registry = VoiceRegistry(default_voice="am_eric")
    server._muted = False
    server._wake_listener = None
    server._listen_active = False
    server._listen_cancel_event = None
    server._session_info = {"name": "Eric", "voice": "am_eric"}
    server._voice_floor = make_floor(tmp_path, "server")
    server._play_ready_sound = lambda: None  # don't afplay during tests

    server._config = MagicMock()
    server._config.coordination.enabled = True
    server._config.coordination.floor_wait = 0.2
    server._config.tts.duck_media = False
    server._config.stt.nudge_on_timeout = False
    server._config.last_voice_name = None
    server._config.main_agent = "Eric"
    return server


class TestServerFloorIntegration:
    @pytest.mark.asyncio
    async def test_speak_busy_returns_floor_busy(self, tmp_path):
        server = _setup_floor_server(tmp_path)
        other = make_floor(tmp_path, "server")  # same paths = another process
        other.acquire_sync("listen", "Nova")

        result = await server.speak("Eric", "Hello there", block=True)

        assert result["success"] is False
        assert result["error"] == "floor_busy"
        assert result["held_by"]["name"] == "Nova"
        assert "Nova" in result["message"]
        other.release()

    @pytest.mark.asyncio
    async def test_speak_acquires_and_releases(self, tmp_path):
        server = _setup_floor_server(tmp_path)
        result = await server.speak("Eric", "Hello there", block=True)

        assert result["success"] is True
        assert server._voice_floor.holder_info() is None  # released after

    @pytest.mark.asyncio
    async def test_speak_then_listen_busy_shape(self, tmp_path):
        server = _setup_floor_server(tmp_path, tts=True)
        other = make_floor(tmp_path, "server")
        other.acquire_sync("speak", "Nova")

        result = await server.speak_then_listen("Eric", "What do you think?")

        assert result["speak"]["error"] == "floor_busy"
        assert result["listen"] == {"success": False, "error": "skipped"}
        other.release()

    @pytest.mark.asyncio
    async def test_speak_then_listen_holds_across_both(self, tmp_path):
        """The floor must be held for the whole op, not just the speak half."""
        server = _setup_floor_server(tmp_path, tts=True, stt=True)
        held_during = []

        # A spy "other process" checks the floor while speak_then_listen runs.
        other = make_floor(tmp_path, "server")
        original_speak = server.speak

        async def spying_speak(*args, **kwargs):
            held_during.append(other.holder_info() is not None)
            return await original_speak(*args, **kwargs)

        server.speak = spying_speak

        result = await server.speak_then_listen("Eric", "What's up?")

        assert result["speak"]["success"] is True
        assert result["listen"]["success"] is True
        assert held_during == [True]  # floor was already held before inner speak
        assert server._voice_floor.holder_info() is None  # released after

    @pytest.mark.asyncio
    async def test_listen_busy_returns_floor_busy(self, tmp_path):
        server = _setup_floor_server(tmp_path, tts=True, stt=True)
        other = make_floor(tmp_path, "server")
        other.acquire_sync("speak", "Nova")

        result = await server.listen(timeout=5)

        assert result["success"] is False
        assert result["error"] == "floor_busy"
        assert result["held_by"]["purpose"] == "speak"
        other.release()

    @pytest.mark.asyncio
    async def test_listen_acquires_and_releases(self, tmp_path):
        server = _setup_floor_server(tmp_path, tts=True, stt=True)
        result = await server.listen(timeout=5)

        assert result["success"] is True
        assert server._voice_floor.holder_info() is None

    @pytest.mark.asyncio
    async def test_status_reports_floor(self, tmp_path):
        server = _setup_floor_server(tmp_path)
        result = await server.status()
        assert result["floor"] == {"held": False}

        server._voice_floor.acquire_sync("listen", "Eric")
        result = await server.status()
        assert result["floor"]["held"] is True
        assert result["floor"]["name"] == "Eric"
        server._voice_floor.release()

    @pytest.mark.asyncio
    async def test_floor_disabled_via_config(self, tmp_path):
        server = _setup_floor_server(tmp_path)
        server._config.coordination.enabled = False
        other = make_floor(tmp_path, "server")
        other.acquire_sync("listen", "Nova")

        result = await server.speak("Eric", "Hello", block=True)
        # With the floor disabled, speak proceeds regardless (legacy behavior).
        assert result["success"] is True
        other.release()

    @pytest.mark.asyncio
    async def test_background_speak_queues_on_floor(self, tmp_path):
        server = _setup_floor_server(tmp_path)
        other = make_floor(tmp_path, "server")
        other.acquire_sync("listen", "Nova")

        result = await server.speak("Eric", "Queued message", block=False)
        assert result["queued"] is True

        # Give the background task a moment to hit the busy floor and drop.
        await asyncio.sleep(0.5)
        assert server._voice_floor.holder_info()["name"] == "Nova"  # holder unchanged
        other.release()
        await asyncio.sleep(0.3)
        assert server._voice_floor.holder_info() is None  # bg speak took + released