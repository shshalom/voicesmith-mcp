"""macOS media ducking via osascript.

Pauses media apps (Apple Music, Spotify) before VoiceSmith audio playback and
resumes them afterward.  No-ops on non-macOS systems.

Usage:
    paused = duck()        # pause whatever is playing; returns list of app names
    ...play audio...
    unduck(paused)         # resume only what we paused
"""

import platform
import subprocess

from shared import get_logger

logger = get_logger("tts.media_duck")

# (display name, AppleScript target name)
_APPS = [
    ("Apple Music", "Music"),
    ("Spotify",     "Spotify"),
]


def _osascript(script: str) -> str:
    """Run a one-liner AppleScript; return stdout stripped, or '' on any error."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def duck() -> list[str]:
    """Pause any playing media apps.

    Returns:
        List of AppleScript target names that were paused (pass to unduck()).
    """
    if platform.system() != "Darwin":
        return []

    paused: list[str] = []
    for display_name, target in _APPS:
        if _osascript(f'application "{target}" is running') != "true":
            continue
        if _osascript(f'tell application "{target}" to get player state') == "playing":
            _osascript(f'tell application "{target}" to pause')
            paused.append(target)
            logger.debug(f"Ducked {display_name}")

    return paused


def unduck(paused: list[str]) -> None:
    """Resume apps that were paused by duck().

    Args:
        paused: The list returned by a previous duck() call.
    """
    if platform.system() != "Darwin":
        return

    for target in paused:
        _osascript(f'tell application "{target}" to play')
        logger.debug(f"Unducked {target}")
