"""
Session registry for multi-session coordination.

Tracks active MCP server sessions in a shared JSON file.
Uses flock for safe concurrent access.
"""

import fcntl
import json
import os
import signal
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from shared import (
    SESSIONS_FILE_NAME,
    DEFAULT_HTTP_PORT,
    VOICE_NAME_MAP,
    ALL_VOICE_IDS,
    get_logger,
)

logger = get_logger("session-registry")


def _sessions_path() -> Path:
    """Return the path to the sessions file."""
    return Path.home() / ".local" / "share" / "voicesmith-mcp" / SESSIONS_FILE_NAME


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_sessions(path: Path) -> list[dict]:
    """Read sessions from file (caller must hold flock)."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("sessions", [])
    except (json.JSONDecodeError, OSError):
        return []


def _write_sessions(path: Path, sessions: list[dict]) -> None:
    """Write sessions to file (caller must hold flock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"sessions": sessions}, f, indent=2)


# Max seconds since last MCP tool call before considering a session stale.
# If a server hasn't had any tool calls in this time, it's likely orphaned
# (MCP client disconnected but process is still running).
_STALE_ACTIVITY_THRESHOLD = 300  # 5 minutes


def _session_healthy(session: dict) -> bool:
    """Check if a session is alive and actively used.

    Three checks:
    1. PID alive (fast, catches crashed processes)
    2. HTTP health check on /status (catches completely dead servers)
    3. Activity check — if last_tool_call_age_s exceeds threshold, the server
       is alive but orphaned (no MCP client connected)
    """
    pid = session.get("pid", 0)
    if not _pid_alive(pid):
        return False

    # Skip further checks for our own PID (we know we're alive)
    if pid == os.getpid():
        return True

    port = session.get("port")
    if not port:
        return False

    try:
        url = f"http://127.0.0.1:{port}/status"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if not data.get("ready", False):
                raise ValueError("not ready")

            mcp_connected = data.get("mcp_connected", True)
            uptime = data.get("uptime_s", 0)

            # A server that has been running >10s but never had an MCP client
            # connect is orphaned — it was started but the client disconnected
            # before making any tool calls (e.g., interrupted resume).
            if not mcp_connected and uptime > 10:
                logger.info(
                    f"Session '{session.get('name')}' (pid {pid}) never connected "
                    f"to MCP client after {uptime}s — treating as stale"
                )
                raise ValueError("never connected")

            # For periodic cleanup (non-aggressive), also check long inactivity
            # This catches servers whose MCP client disconnected long ago
            # but the process is still lingering
            age = data.get("last_tool_call_age_s")
            if age is not None and age > _STALE_ACTIVITY_THRESHOLD:
                logger.info(
                    f"Session '{session.get('name')}' (pid {pid}) inactive "
                    f"for {age}s — treating as stale"
                )
                raise ValueError("inactive")

            return True
    except Exception:
        # Server not responding or inactive — it's orphaned
        logger.info(f"Session '{session.get('name')}' (pid {pid}) stale on port {port}")
        # Kill the orphaned process
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info(f"Sent SIGTERM to orphaned process {pid}")
        except OSError:
            pass
        return False


def _clean_stale(sessions: list[dict]) -> list[dict]:
    """Remove sessions that are dead or unresponsive.

    Detection logic:
    1. PID dead → remove immediately
    2. HTTP /status not responding → remove and kill
    3. Server running >10s but MCP client never connected → orphaned, remove and kill
    4. Server with MCP connected but inactive >5min → orphaned, remove and kill
    """
    alive = []
    for s in sessions:
        if _session_healthy(s):
            alive.append(s)
        else:
            logger.info(f"Removed stale session: {s.get('name')} (pid {s.get('pid')})")
    return alive


# Voice name priority: American English first, then British, then the rest.
# Within each group, order is curated (not alphabetical).
_VOICE_PRIORITY = [
    # American English - Male
    ("adam", "am_adam"), ("echo", "am_echo"), ("eric", "am_eric"),
    ("fenrir", "am_fenrir"), ("liam", "am_liam"), ("michael", "am_michael"),
    ("onyx", "am_onyx"), ("puck", "am_puck"),
    # American English - Female
    ("nova", "af_nova"), ("bella", "af_bella"), ("heart", "af_heart"),
    ("jessica", "af_jessica"), ("nicole", "af_nicole"), ("river", "af_river"),
    ("sarah", "af_sarah"), ("sky", "af_sky"), ("alloy", "af_alloy"),
    ("aoede", "af_aoede"), ("kore", "af_kore"),
    # British English - Male
    ("daniel", "bm_daniel"), ("fable", "bm_fable"),
    ("george", "bm_george"), ("lewis", "bm_lewis"),
    # British English - Female
    ("alice", "bf_alice"), ("emma", "bf_emma"),
    ("isabella", "bf_isabella"), ("lily", "bf_lily"),
    # Everything else (Spanish, French, Hindi, Italian, Japanese, etc.)
    ("alex", "em_alex"), ("dora", "ef_dora"), ("siwis", "ff_siwis"),
    ("alpha", "hf_alpha"), ("beta", "hf_beta"), ("omega", "hm_omega"),
    ("psi", "hm_psi"), ("sara", "if_sara"), ("nicola", "im_nicola"),
    ("gongitsune", "jf_gongitsune"), ("nezumi", "jf_nezumi"),
    ("tebukuro", "jf_tebukuro"), ("kumo", "jm_kumo"),
    ("xiaobei", "zf_xiaobei"), ("xiaoni", "zf_xiaoni"),
    ("xiaoxiao", "zf_xiaoxiao"), ("xiaoyi", "zf_xiaoyi"),
    ("yunjian", "zm_yunjian"), ("yunxi", "zm_yunxi"),
    ("yunxia", "zm_yunxia"), ("yunyang", "zm_yunyang"),
    # Santa voices last (novelty)
    ("santa", "am_santa"),
]


def _find_available_name(taken_names: set[str], preferred: str) -> tuple[str, str]:
    """Find an available voice name.

    Returns (name, voice_id). Tries the preferred name first,
    then picks from the priority-ordered voice list (American English first,
    then British English, then everything else).
    """
    # Try preferred name
    preferred_lower = preferred.lower()
    if preferred not in taken_names and preferred_lower in VOICE_NAME_MAP:
        return preferred, VOICE_NAME_MAP[preferred_lower]

    # Pick the first available name from the priority list
    for name_lower, voice_id in _VOICE_PRIORITY:
        name = name_lower.capitalize()
        if name not in taken_names:
            return name, voice_id

    # All names taken — shouldn't happen with 54 voices, but fallback
    return preferred, VOICE_NAME_MAP.get(preferred_lower, "am_eric")


def _find_available_port(sessions: list[dict], base_port: int) -> int:
    """Find the lowest available port starting from base_port."""
    used_ports = {s.get("port") for s in sessions}
    port = base_port
    while port in used_ports:
        port += 1
    return port


def register_session(
    preferred_name: str,
    preferred_voice: str,
    base_port: int = DEFAULT_HTTP_PORT,
    tmux_session: Optional[str] = None,
) -> dict:
    """Register this server as an active session.

    Returns the session dict with assigned name, voice, and port.
    Uses flock on sessions.json for safe concurrent access.
    """
    path = _sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Touch the file so flock has something to lock
    if not path.exists():
        _write_sessions(path, [])

    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)

        sessions = _read_sessions(path)
        # Aggressive cleanup on startup — use short activity threshold
        # to quickly reclaim names from orphaned servers
        sessions = _clean_stale(sessions)

        taken_names = {s["name"] for s in sessions}

        if preferred_name in taken_names:
            # Wait briefly and retry — the old server may be shutting down
            fcntl.flock(f, fcntl.LOCK_UN)
            time.sleep(2)
            fcntl.flock(f, fcntl.LOCK_EX)
            sessions = _read_sessions(path)
            sessions = _clean_stale(sessions)
            _write_sessions(path, sessions)
            taken_names = {s["name"] for s in sessions}

        if preferred_name in taken_names:
            name, voice = _find_available_name(taken_names, preferred_name)
            logger.warning(
                f"'{preferred_name}' already active. Assigned '{name}' ({voice}) instead."
            )
        else:
            name = preferred_name
            voice = preferred_voice

        port = _find_available_port(sessions, base_port)

        # Read tmux session from env if not provided
        if tmux_session is None:
            tmux_session = os.environ.get("VOICESMITH_TMUX")

        session = {
            "name": name,
            "voice": voice,
            "port": port,
            "pid": os.getpid(),
            "tmux_session": tmux_session,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        sessions.append(session)
        _write_sessions(path, sessions)

        # flock released when f closes

    logger.info(f"Session registered: {name} ({voice}) on port {port}")
    return session


def unregister_session() -> None:
    """Remove this server's session from the registry."""
    path = _sessions_path()
    if not path.exists():
        return

    pid = os.getpid()

    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            sessions = _read_sessions(path)
            sessions = [s for s in sessions if s.get("pid") != pid]
            _write_sessions(path, sessions)
    except OSError as e:
        logger.warning(f"Failed to unregister session: {e}")

    logger.info("Session unregistered")


def get_active_sessions() -> list[dict]:
    """Return list of active sessions (stale PIDs filtered out)."""
    path = _sessions_path()
    if not path.exists():
        return []

    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            sessions = _read_sessions(path)
            alive = _clean_stale(sessions)
            if len(alive) != len(sessions):
                _write_sessions(path, alive)
            return alive
    except OSError:
        return []
