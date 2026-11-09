"""Tests for session registry health checks and orphan detection."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import session_registry as sr


SESSION = {"name": "Eric", "voice": "am_eric", "pid": 12345}


class TestOrphanDetection:
    def test_ps_failure_never_orphan_kills(self, monkeypatch):
        """A transient `ps` failure (-1) must keep the session alive.

        Regression: _get_ppid used to return 0 on failure, and the
        `ppid <= 1` check treated that as orphaned — one ps hiccup under
        load mass-SIGTERMed healthy sessions and wiped the registry.
        """
        killed = []
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: -1)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert sr._session_healthy(SESSION) is True
        assert killed == []  # no SIGTERM on a failed probe

    def test_sandbox_eperm_never_means_dead(self, monkeypatch):
        """EPERM from os.kill (T3 seatbelt sandbox) must fall back to ps.

        Regression: sandboxed cleanup passes read EPERM as "dead" and
        wiped the whole registry while every session was alive.
        """
        monkeypatch.setattr(sr.os, "kill", self._eperm)
        monkeypatch.setattr(sr, "_ps_pid_exists", lambda pid: True)

        assert sr._pid_alive(12345) is True

    def test_sandbox_eperm_with_ps_confirmed_dead(self, monkeypatch):
        monkeypatch.setattr(sr.os, "kill", self._eperm)
        monkeypatch.setattr(sr, "_ps_pid_exists", lambda pid: False)
        assert sr._pid_alive(12345) is False

    @staticmethod
    def _eperm(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    def test_ps_pid_exists_semantics(self, monkeypatch):
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 1
                stdout = ""
            return R()

        monkeypatch.setattr(sp, "run", fake_run)
        assert sr._ps_pid_exists(12345) is False  # confirmed absent

        def fake_run_ok(cmd, **kwargs):
            class R:
                returncode = 0
                stdout = "12345\n"
            return R()

        monkeypatch.setattr(sp, "run", fake_run_ok)
        assert sr._ps_pid_exists(12345) is True

        def fake_run_broken(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 2)

        monkeypatch.setattr(sp, "run", fake_run_broken)
        assert sr._ps_pid_exists(12345) is True  # unknown → keep

    def test_wiped_registry_survives_sandboxed_pass(self, tmp_path, monkeypatch):
        """A sandboxed cleaner (EPERM on all probes) removes nothing."""
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": [{"name": "Eric", "pid": 12345}, '
                        '{"name": "Nova", "pid": 54321}]}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)
        monkeypatch.setattr(sr.os, "kill", self._eperm)
        monkeypatch.setattr(sr, "_ps_pid_exists", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: -1)

        sessions = sr.get_active_sessions()
        assert [s["name"] for s in sessions] == ["Eric", "Nova"]

    def test_set_session_thread_id_updates_existing(self, tmp_path, monkeypatch):
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": [{"name": "Eric", "pid": 12345}]}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)

        updated = sr.set_session_thread_id(12345, "thread-abc")
        assert updated["t3_thread_id"] == "thread-abc"
        assert json.loads(path.read_text())["sessions"][0]["t3_thread_id"] == "thread-abc"

    def test_set_session_thread_id_fallback_creates(self, tmp_path, monkeypatch):
        """A wiped entry is re-created from the session's own metadata."""
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": []}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)

        updated = sr.set_session_thread_id(
            12345, "thread-abc",
            fallback_session={"name": "Liam", "pid": 12345, "port": 7870},
        )
        assert updated["t3_thread_id"] == "thread-abc"
        assert updated["name"] == "Liam"
        stored = json.loads(path.read_text())["sessions"]
        assert len(stored) == 1
        assert stored[0]["pid"] == 12345
        assert stored[0]["t3_thread_id"] == "thread-abc"

    def test_set_session_thread_id_missing_without_fallback(self, tmp_path, monkeypatch):
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": []}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)
        assert sr.set_session_thread_id(12345, "x") is None

    def test_set_session_thread_id_recovers_wiped_file(self, tmp_path, monkeypatch):
        """Registry file gone entirely (wipe mid-write) still works."""
        path = tmp_path / "sessions.json"
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)

        updated = sr.set_session_thread_id(
            12345, "thread-abc",
            fallback_session={"name": "Liam", "pid": 12345},
        )
        assert updated["t3_thread_id"] == "thread-abc"
        assert path.exists()

    def test_confirmed_orphan_is_killed(self, monkeypatch):
        """A confirmed reparent to launchd (ppid 1) is killed and stale."""
        killed = []
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: 1)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert sr._session_healthy(SESSION) is False
        assert killed == [(12345, 15)]  # SIGTERM=15

    def test_live_parent_keeps_session(self, monkeypatch):
        """A real parent pid means the IDE still runs — keep the session."""
        killed = []
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: 38384)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert sr._session_healthy(SESSION) is True
        assert killed == []

    def test_dead_pid_is_stale_without_sigterm(self, monkeypatch):
        """Dead pids are stale; no signal is sent to them."""
        killed = []
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert sr._session_healthy(SESSION) is False
        assert killed == []

    def test_own_pid_skips_ppid_check(self, monkeypatch):
        """Our own session is healthy without any ps probe."""
        probed = []
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: probed.append(pid) or -1)

        assert sr._session_healthy({"pid": os.getpid()}) is True
        assert probed == []  # no ps spawned for ourselves

    def test_clean_stale_preserves_entries_on_probe_failure(self, tmp_path, monkeypatch):
        """get_active_sessions under a failed ps pass removes nothing."""
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": [{"name": "Eric", "pid": 12345}]}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: -1)

        sessions = sr.get_active_sessions()
        assert len(sessions) == 1
        assert sessions[0]["name"] == "Eric"

    def test_clean_stale_removes_confirmed_orphans(self, tmp_path, monkeypatch):
        path = tmp_path / "sessions.json"
        path.write_text('{"sessions": [{"name": "Eric", "pid": 12345}, '
                        '{"name": "Nova", "pid": 54321}]}')
        monkeypatch.setattr(sr, "_sessions_path", lambda: path)
        monkeypatch.setattr(sr, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(sr, "_get_ppid", lambda pid: 1 if pid == 12345 else 99999)

        sessions = sr.get_active_sessions()
        assert [s["name"] for s in sessions] == ["Nova"]