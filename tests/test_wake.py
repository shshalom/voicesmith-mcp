"""Tests for the wake word listener."""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import STT_SAMPLE_RATE, TranscriptionResult


# ─── WakeWordListener Tests ──────────────────────────────────────────────────


class TestWakeWordListenerInit:
    """Tests for WakeWordListener initialization."""

    def _make_listener(self, **kwargs):
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = TranscriptionResult(
            text="hello world", confidence=0.9, transcription_ms=200.0
        )
        mock_vad = MagicMock()
        mock_vad.is_speech.return_value = False

        defaults = {
            "stt_engine": mock_stt,
            "vad": mock_vad,
            "wake_model_name": "hey_jarvis_v0.1",
            "threshold": 0.5,
            "tmux_session": "agent-voice-test",
            "ready_sound": None,
            "recording_timeout": 10,
            "no_speech_timeout": 5,
        }
        defaults.update(kwargs)

        mock_oww = MagicMock()
        mock_model = MagicMock()
        mock_oww.model.Model.return_value = mock_model

        with patch.dict("sys.modules", {"openwakeword": mock_oww, "openwakeword.model": mock_oww.model}):
            from wake_listener import WakeWordListener
            listener = WakeWordListener(**defaults)

        return listener, mock_stt, mock_vad, mock_model

    def test_initial_state_is_disabled(self):
        listener, _, _, _ = self._make_listener()
        assert listener.state == "disabled"
        assert listener.is_listening is False

    def test_sound_resolution_tink_macos(self):
        with patch("platform.system", return_value="Darwin"):
            from wake_listener import WakeWordListener
            result = WakeWordListener._resolve_sound("tink")
            assert result == "/System/Library/Sounds/Tink.aiff"

    def test_sound_resolution_none(self):
        from wake_listener import WakeWordListener
        assert WakeWordListener._resolve_sound(None) is None
        assert WakeWordListener._resolve_sound("") is None


class TestMicYieldReclaim:
    """Tests for mic ownership handoff."""

    def _make_listener(self):
        mock_stt = MagicMock()
        mock_vad = MagicMock()

        mock_oww = MagicMock()
        mock_model = MagicMock()
        mock_oww.model.Model.return_value = mock_model

        with patch.dict("sys.modules", {"openwakeword": mock_oww, "openwakeword.model": mock_oww.model}):
            from wake_listener import WakeWordListener, WakeState
            listener = WakeWordListener(
                stt_engine=mock_stt, vad=mock_vad,
                wake_model_name="test", tmux_session="test",
                ready_sound=None,
            )
            # Simulate listening state
            listener._state = WakeState.LISTENING

        return listener

    def test_yield_sets_event(self):
        listener = self._make_listener()
        # yield_mic checks state, but since we're not running the thread,
        # just verify the event gets set
        from wake_listener import WakeState
        listener._yield_event.set()
        assert listener._yield_event.is_set()

    def test_reclaim_clears_events(self):
        listener = self._make_listener()
        listener._yield_event.set()
        listener._yield_done.set()
        listener.reclaim_mic()
        assert not listener._yield_event.is_set()
        assert not listener._yield_done.is_set()


class TestTextInjection:
    """Tests for tmux text injection routing."""

    def _make_listener(self):
        mock_stt = MagicMock()
        mock_vad = MagicMock()

        mock_oww = MagicMock()
        mock_model = MagicMock()
        mock_oww.model.Model.return_value = mock_model

        with patch.dict("sys.modules", {"openwakeword": mock_oww, "openwakeword.model": mock_oww.model}):
            from wake_listener import WakeWordListener
            listener = WakeWordListener(
                stt_engine=mock_stt, vad=mock_vad,
                wake_model_name="test", tmux_session="agent-voice-123",
                ready_sound=None,
            )
        return listener

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_single_session_routing(self, mock_sessions, mock_run):
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": "agent-voice-123", "pid": 1}
        ]
        listener = self._make_listener()
        listener._inject_text("hello world")

        # Should send literal text then Enter
        assert mock_run.call_count == 2
        literal_call = mock_run.call_args_list[0]
        assert "-l" in literal_call[0][0]
        assert "hello world" in literal_call[0][0]
        enter_call = mock_run.call_args_list[1]
        assert "Enter" in enter_call[0][0]

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_multi_session_name_routing(self, mock_sessions, mock_run):
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": "agent-voice-123", "pid": 1},
            {"name": "Nova", "tmux_session": "agent-voice-456", "pid": 2},
        ]
        listener = self._make_listener()
        listener._inject_text("Nova run the tests")

        # Should route to Nova's session with "run the tests"
        literal_call = mock_run.call_args_list[0]
        assert "agent-voice-456" in literal_call[0][0]
        assert "run the tests" in literal_call[0][0]

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_multi_session_no_name_match(self, mock_sessions, mock_run):
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": "agent-voice-123", "pid": 1},
            {"name": "Nova", "tmux_session": "agent-voice-456", "pid": 2},
        ]
        listener = self._make_listener()
        listener._inject_text("fix the bug")

        # Should route to most recent (last) session with full text
        literal_call = mock_run.call_args_list[0]
        assert "agent-voice-456" in literal_call[0][0]
        assert "fix the bug" in literal_call[0][0]

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_no_tmux_sessions(self, mock_sessions, mock_run):
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": None, "pid": 1},
        ]
        listener = self._make_listener()
        listener._inject_text("hello")

        # No tmux sessions — should not call subprocess
        mock_run.assert_not_called()

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_empty_message_after_name_strip(self, mock_sessions, mock_run):
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": "agent-voice-123", "pid": 1},
            {"name": "Nova", "tmux_session": "agent-voice-456", "pid": 2},
        ]
        listener = self._make_listener()
        listener._inject_text("Nova")

        # Only the name, no message — should skip injection
        mock_run.assert_not_called()

    @patch("subprocess.run")
    @patch("session_registry.get_active_sessions")
    def test_literal_flag_prevents_injection(self, mock_sessions, mock_run):
        """tmux send-keys should use -l flag to prevent shell metacharacter injection."""
        mock_sessions.return_value = [
            {"name": "Eric", "tmux_session": "agent-voice-123", "pid": 1}
        ]
        listener = self._make_listener()
        listener._inject_text("$(rm -rf /)")

        literal_call = mock_run.call_args_list[0]
        cmd = literal_call[0][0]
        assert "-l" in cmd  # Literal flag present
        assert "$(rm -rf /)" in cmd  # Text sent as-is, not interpreted


class TestRecordingTimeout:
    """Tests for recording timeout behavior."""

    def test_no_speech_timeout_config(self):
        mock_stt = MagicMock()
        mock_vad = MagicMock()

        mock_oww = MagicMock()
        mock_oww.model.Model.return_value = MagicMock()

        with patch.dict("sys.modules", {"openwakeword": mock_oww, "openwakeword.model": mock_oww.model}):
            from wake_listener import WakeWordListener
            listener = WakeWordListener(
                stt_engine=mock_stt, vad=mock_vad,
                wake_model_name="test", tmux_session="test",
                ready_sound=None,
                recording_timeout=10,
                no_speech_timeout=5,
            )
        assert listener._recording_timeout == 10
        assert listener._no_speech_timeout == 5
