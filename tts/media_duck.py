"""macOS media ducking via osascript.

Pauses media apps (Apple Music, Spotify) and browser tabs (Chrome, Brave,
Edge, Safari) before VoiceSmith audio playback and resumes them afterward.
No-ops on non-macOS systems.

Browser ducking uses JavaScript injection via AppleScript.  The first time
each browser is targeted, macOS will prompt for Automation permission — approve
once and it is remembered.

Usage:
    paused = duck()        # pause everything playing; returns opaque token list
    ...play audio...
    unduck(paused)         # resume only what we paused
"""

import ctypes
import ctypes.util
import platform
import subprocess

from shared import get_logger

logger = get_logger("tts.media_duck")

# ── Native media apps ─────────────────────────────────────────────────────────

# (display name, AppleScript target)
_APPS = [
    ("Apple Music", "Music"),
    ("Spotify",     "Spotify"),
]

# ── Browsers ──────────────────────────────────────────────────────────────────

# (display name, AppleScript target, family: "chrome" | "safari")
_BROWSERS = [
    ("Google Chrome",   "Google Chrome",   "chrome"),
    ("Brave Browser",   "Brave Browser",   "chrome"),
    ("Microsoft Edge",  "Microsoft Edge",  "chrome"),
    ("Safari",          "Safari",          "safari"),
]

# JS injected into every tab on duck: pause playing media and mark it.
_JS_PAUSE = (
    "document.querySelectorAll('video,audio').forEach(function(v){"
    "if(!v.paused){v.pause();v.dataset.voicesmithPaused='1'}"
    "})"
)

# JS injected on unduck: resume only elements we marked, then clear the mark.
_JS_RESUME = (
    "document.querySelectorAll('video,audio').forEach(function(v){"
    "if(v.dataset.voicesmithPaused){delete v.dataset.voicesmithPaused;v.play()}"
    "})"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _osascript(script: str) -> str:
    """Run an AppleScript (may be multi-line); return stdout stripped, or '' on error."""
    try:
        result = subprocess.run(
            ["osascript"],
            input=script,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _browser_script(target: str, family: str, js: str) -> str:
    """Build an AppleScript that runs js in every tab of target browser."""
    if family == "safari":
        exec_stmt = f'do JavaScript "{js}" in t'
    else:  # chrome family
        exec_stmt = f'execute t javascript "{js}"'

    return f"""\
tell application "{target}"
    repeat with w in windows
        repeat with t in tabs of w
            try
                {exec_stmt}
            end try
        end repeat
    end repeat
end tell"""


# ── Bluetooth detection (macOS CoreAudio) ────────────────────────────────────

def is_bluetooth_output() -> bool:
    """Return True if the default audio output is a Bluetooth device.

    Uses CoreAudio's AudioObjectGetPropertyData to check the transport type
    of the default output device.  Returns False on non-macOS or on error.
    """
    if platform.system() != "Darwin":
        return False

    try:
        lib_path = ctypes.util.find_library("CoreAudio")
        if not lib_path:
            return False
        ca = ctypes.cdll.LoadLibrary(lib_path)

        class _AudioObjectPropertyAddress(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        # CoreAudio FourCC constants
        _SYS_OBJ   = 1                                            # kAudioObjectSystemObject
        _SCOPE_G   = int.from_bytes(b"glob", "big")               # kAudioObjectPropertyScopeGlobal
        _ELEM_M    = 0                                             # kAudioObjectPropertyElementMain
        _DEF_OUT   = int.from_bytes(b"dOut", "big")                # kAudioHardwarePropertyDefaultOutputDevice
        _TRANS     = int.from_bytes(b"tran", "big")                # kAudioDevicePropertyTransportType
        _BT        = int.from_bytes(b"blue", "big")                # kAudioDeviceTransportTypeBluetooth
        _BT_LE     = int.from_bytes(b"blea", "big")                # kAudioDeviceTransportTypeBluetoothLE

        # Get default output device ID
        addr = _AudioObjectPropertyAddress(_DEF_OUT, _SCOPE_G, _ELEM_M)
        device_id = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        err = ca.AudioObjectGetPropertyData(
            _SYS_OBJ, ctypes.byref(addr), 0, None,
            ctypes.byref(size), ctypes.byref(device_id),
        )
        if err != 0:
            return False

        # Get transport type of that device
        addr.mSelector = _TRANS
        transport = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        err = ca.AudioObjectGetPropertyData(
            device_id.value, ctypes.byref(addr), 0, None,
            ctypes.byref(size), ctypes.byref(transport),
        )
        if err != 0:
            return False

        return transport.value in (_BT, _BT_LE)
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def duck() -> list[str]:
    """Pause any playing media apps and browser tabs.

    Returns:
        Opaque list of tokens — pass unchanged to unduck().
    """
    if platform.system() != "Darwin":
        return []

    paused: list[str] = []

    # Native apps (Music, Spotify)
    for display_name, target in _APPS:
        if _osascript(f'application "{target}" is running') != "true":
            continue
        if _osascript(f'tell application "{target}" to get player state') == "playing":
            _osascript(f'tell application "{target}" to pause')
            paused.append(target)
            logger.debug(f"Ducked {display_name}")

    # Browsers — inject pause JS into every tab
    for display_name, target, family in _BROWSERS:
        if _osascript(f'application "{target}" is running') != "true":
            continue
        _osascript(_browser_script(target, family, _JS_PAUSE))
        paused.append(f"browser:{target}")
        logger.debug(f"Ducked browser tabs in {display_name}")

    return paused


def unduck(paused: list[str]) -> None:
    """Resume apps and browser tabs paused by duck().

    Args:
        paused: The list returned by a previous duck() call.
    """
    if platform.system() != "Darwin":
        return

    for token in paused:
        if token.startswith("browser:"):
            target = token[len("browser:"):]
            # family lookup for resume script
            family = next(
                (f for _, t, f in _BROWSERS if t == target),
                "chrome",
            )
            _osascript(_browser_script(target, family, _JS_RESUME))
            logger.debug(f"Unducked browser tabs in {target}")
        else:
            _osascript(f'tell application "{token}" to play')
            logger.debug(f"Unducked {token}")
