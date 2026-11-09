"""
Shared cross-session state: global mute flags and volume.

Every MCP session is a separate process; the menu bar app and the
sessions coordinate through a small JSON state file in the data dir.
Global settings apply to ALL sessions — including ones spawned after the
setting was changed (unlike the old menu-bar "mute all" that POSTed to a
snapshot of live sessions and missed anything spawned later, like
sub-agents).

File: ~/.local/share/voicesmith-mcp/state.json
Lock: state.json.lock (never replaced — writers flock it, readers don't
need it because writes are atomic replace).

Fields:
- mute_all (bool)      — no session speaks or listens (meeting mode)
- mute_subagents (bool) — agent-spawned sessions only (see server's
                          subagent detection); user's main threads unaffected
- volume (float 0.0-1.0) — global VoiceSmith playback gain; sessions with
                          their own override use that instead
"""

import fcntl
import json
import os
from pathlib import Path

from shared import STATE_FILE_NAME, get_logger

logger = get_logger("server-state")

DEFAULTS = {
    "mute_all": False,
    "mute_subagents": False,
    "volume": 1.0,
}


def _data_dir() -> Path:
    return Path.home() / ".local" / "share" / "voicesmith-mcp"


def _state_path() -> Path:
    return _data_dir() / STATE_FILE_NAME


def _lock_path() -> Path:
    return _data_dir() / (STATE_FILE_NAME + ".lock")


def read_state() -> dict:
    """Read the shared state, merged over defaults. Never raises."""
    state = dict(DEFAULTS)
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            state.update(data)
    except (OSError, json.JSONDecodeError):
        pass
    # Clamp volume defensively
    try:
        v = float(state.get("volume", 1.0))
        state["volume"] = min(1.0, max(0.0, v))
    except (TypeError, ValueError):
        state["volume"] = 1.0
    return state


def update_state(**fields) -> dict:
    """Read-modify-write the shared state under flock; returns the new state.

    Blocks until the write completes. Tolerates a missing/corrupt file by
    starting from defaults.
    """
    _data_dir().mkdir(parents=True, exist_ok=True)
    lock = open(_lock_path(), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        for key, value in fields.items():
            if key in DEFAULTS:
                state[key] = clamp_volume(value) if key == "volume" else value
        # Atomic replace so lockless readers always see a consistent file
        tmp = str(_state_path()) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _state_path())
        return state
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def is_globally_muted() -> bool:
    return bool(read_state().get("mute_all"))


def clamp_volume(v) -> float:
    """Clamp a volume to 0.0-1.0; accept 0-100 ints by scaling down."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    if v > 1.0 and v <= 100.0:
        v = v / 100.0  # user/agent passed a percent
    return min(1.0, max(0.0, v))