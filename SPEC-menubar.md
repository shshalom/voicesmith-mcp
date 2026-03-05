# Menu Bar App — Design Spec

## Context

VoiceSmith MCP has grown beyond basic TTS/STT. It now includes media ducking, wake word detection, configurable nudge, multiple mic backends, and multi-session coordination. Users need a way to see what's happening and control settings without editing config files or asking the AI. A native macOS menu bar app provides always-visible status and one-click controls.

## Overview

- **Platform:** macOS only (matches VoiceSmithMCP.app native launcher)
- **Framework:** `rumps` (Python, lightweight menu bar apps) or SwiftUI (native)
- **Communication:** HTTP polling against each session's `/status` endpoint + direct config.json read/write
- **Install:** Optional — bundled with the installer, enabled via `--with-menubar` flag

---

## Menu Bar Icon

The icon reflects the current mic/voice state at a glance:

| State | Icon | Description |
|-------|------|-------------|
| Idle (no sessions) | 🎙 dim | No active VoiceSmith sessions |
| Active (listening for wake word) | 🎙 normal | Wake word listener is monitoring |
| Recording (speech capture) | 🎙 pulsing | User is speaking, mic is recording |
| AI listening (speak_then_listen) | 🎙 blue | AI asked a question, waiting for response |
| Muted | 🎙 crossed | All voice output silenced |
| Error | 🎙 red | TTS or STT failed to load |

The icon updates by polling active sessions every 2 seconds.

---

## Menu Structure

```
┌─────────────────────────────────────────┐
│  VoiceSmith MCP                    v1.x │
│─────────────────────────────────────────│
│  ● Fenrir (am_fenrir)        port 7865  │  ← current session
│  ○ Nova (af_nova)            port 7866  │  ← other active session
│─────────────────────────────────────────│
│  ✓ Media Ducking                        │  ← toggle
│    Nudge on Timeout                     │  ← toggle
│  ✓ Wake Word                            │  ← toggle
│─────────────────────────────────────────│
│  Voice ►                                │  ← submenu
│  │  ✓ Fenrir (am_fenrir)               │
│  │    Eric (am_eric)                    │
│  │    Nova (af_nova)                    │
│  │    Adam (am_adam)                    │
│  │    ... (all 54 voices)              │
│─────────────────────────────────────────│
│  Whisper Model ►                        │  ← submenu
│  │  ✓ base (~150MB, fastest)           │
│  │    small (~500MB, better accuracy)  │
│  │    medium (~1.5GB, very accurate)   │
│  │    large-v3 (~3GB, best accuracy)   │
│─────────────────────────────────────────│
│  Voice Rules ►                          │  ← submenu
│  │  View Rules...                       │  ← opens in default editor
│  │  Edit Rules...                       │  ← opens in default editor
│  │  Reset to Default                    │  ← re-renders from template
│─────────────────────────────────────────│
│  Server Health ►                        │  ← submenu
│  │  TTS: ● loaded (kokoro-v1.0)       │
│  │  STT: ● loaded (whisper-base)      │
│  │  VAD: ● loaded                      │
│  │  Uptime: 1h 23m                     │
│  │  Queue depth: 0                     │
│─────────────────────────────────────────│
│  Stop Playback                          │  ← action
│  Test Voice                             │  ← action
│─────────────────────────────────────────│
│  Quit VoiceSmith Menu                   │
└─────────────────────────────────────────┘
```

---

## Features

### 1. Session List

Shows all active sessions from `sessions.json`. Each entry displays:
- Session name and voice ID
- HTTP port
- Active indicator (● for current/healthy, ○ for other sessions)
- Clicking a session copies its port to clipboard (for debugging)

**Data source:** Read `~/.local/share/voicesmith-mcp/sessions.json` directly (faster than HTTP, no flock needed for reads).

### 2. Quick Toggles

One-click toggles that update `config.json` and notify active sessions:

| Toggle | Config key | Effect |
|--------|-----------|--------|
| Media Ducking | `tts.duck_media` | Auto-pause music during speech |
| Nudge on Timeout | `stt.nudge_on_timeout` | Speak nudge when listen times out |
| Wake Word | `wake_word.enabled` | Enable/disable wake word listener |

**How toggles work:**
1. Read `config.json`, flip the boolean, write back (atomic write via temp file + rename)
2. For wake word: also call `POST /wake_enable` or `/wake_disable` on the active session's HTTP endpoint to apply immediately without restart

**Note:** Media ducking and nudge changes take effect on the next `speak` / `speak_then_listen` call — no restart needed since the server reads config values at call time. Wake word needs an explicit HTTP call because the listener thread must be started/stopped.

### 3. Voice Switcher

Submenu listing all 54 Kokoro voices, grouped by language:
- **American English** (20 voices)
- **British English** (8 voices)
- **Other Languages** (26 voices)

Current voice has a checkmark. Clicking a voice:
1. Calls `POST /set_voice` on the active session's HTTP endpoint with `{"voice": "am_fenrir"}`
2. Session renames itself (name + voice + registry + config all updated)
3. Menu updates to show new name

**Requires:** New HTTP endpoint `POST /set_voice` on the server (currently only available as MCP tool).

### 4. Whisper Model Switcher

Submenu showing available model sizes with current selection checkmarked:

| Model | Size | Speed | When to use |
|-------|------|-------|-------------|
| base | ~150MB | ~0.2s | Default — fast, good for clear speech |
| small | ~500MB | ~0.5s | Accented speech, noisy environments |
| medium | ~1.5GB | ~1.5s | Complex sentences, multiple languages |
| large-v3 | ~3GB | ~3s | Maximum accuracy |

Clicking a model:
1. Updates `stt.model_size` in `config.json`
2. Shows a notification: "Whisper model changed to 'small'. Restart your session to apply."
3. The model downloads automatically on next session startup (faster-whisper handles this)

**Note:** Model switch requires server restart because faster-whisper loads the model once at startup. The menu bar app cannot hot-swap models.

### 5. Voice Rules

| Action | What it does |
|--------|-------------|
| **View Rules** | Opens the installed voice rules file in the default text editor (read-only intent). Path depends on IDE: `~/.claude/CLAUDE.md` for Claude Code, `~/.cursor/rules/voicesmith.mdc` for Cursor. |
| **Edit Rules** | Same as View — opens in editor. The file is user-editable. |
| **Reset to Default** | Re-renders `templates/voice-rules.md` with the current `main_agent` name from config, and overwrites the sentinel-marked block in the IDE config file. Shows confirmation dialog first. |

**Reset logic:**
1. Read `main_agent` from `config.json`
2. Read `templates/voice-rules.md` from the install directory (`~/.local/share/voicesmith-mcp/templates/voice-rules.md`)
3. Replace `{{MAIN_AGENT}}` with the main agent name
4. Find the sentinel block (`<!-- installed by voicesmith-mcp -->`) in each IDE config file
5. Replace the block content with the freshly rendered template
6. Show notification: "Voice rules reset to default for Claude Code"

**IDE detection:** Check which config files exist:
- `~/.claude/CLAUDE.md` → Claude Code
- `~/.cursor/rules/voicesmith.mdc` → Cursor
- `~/.codex/AGENTS.md` → Codex

If multiple IDEs are installed, reset all of them (with confirmation listing which files will be updated).

### 6. Server Health

Read-only submenu showing component status. Polled from the active session's HTTP `/status` endpoint.

| Item | Source | Display |
|------|--------|---------|
| TTS | `tts.loaded` | Green/red dot + model name |
| STT | `stt.loaded` | Green/red dot + model name |
| VAD | `vad.loaded` | Green/red dot |
| Uptime | `uptime_s` | Human-readable (e.g., "1h 23m") |
| Queue depth | `queue_depth` | Number of pending speech items |
| Wake word state | `wake_word.state` | listening / recording / yielded / disabled |

### 7. Actions

| Action | What it does |
|--------|-------------|
| **Stop Playback** | Calls `POST /stop` on the active session — stops any playing audio and cancels active listen |
| **Test Voice** | Calls `POST /speak` on the active session with `{"text": "VoiceSmith is working.", "name": "<current_name>"}` |

---

## HTTP API Changes Required

The current HTTP API is minimal (GET `/status`, POST `/listen`, `/speak`, `/session`). The menu bar app needs:

| Endpoint | Method | Body | Purpose |
|----------|--------|------|---------|
| `/status` | GET | — | **Extend** to include full status (TTS/STT/VAD loaded, muted, wake state, config values) |
| `/set_voice` | POST | `{"voice": "am_fenrir"}` | **New** — mirrors MCP `set_voice` tool |
| `/stop` | POST | — | **New** — mirrors MCP `stop` tool |
| `/mute` | POST | — | **New** — mirrors MCP `mute` tool |
| `/unmute` | POST | — | **New** — mirrors MCP `unmute` tool |
| `/wake_enable` | POST | — | Already exists (if wake word installed) |
| `/wake_disable` | POST | — | Already exists (if wake word installed) |

The new endpoints bridge HTTP to the same async functions the MCP tools call, using `asyncio.run_coroutine_threadsafe()` (same pattern as the existing `/listen` handler).

### Extended `/status` Response

```json
{
  "ready": true,
  "name": "Fenrir",
  "voice": "am_fenrir",
  "port": 7865,
  "session_id": "abc-123",
  "mcp_connected": true,
  "uptime_s": 4980,
  "last_tool_call_age_s": 12,
  "muted": false,
  "tts": {
    "loaded": true,
    "model": "kokoro-v1.0.onnx",
    "voices": 54,
    "duck_media": true
  },
  "stt": {
    "loaded": true,
    "model": "whisper-base",
    "language": "en",
    "nudge_on_timeout": false
  },
  "vad": {
    "loaded": true
  },
  "wake_word": {
    "enabled": true,
    "listening": true,
    "state": "listening",
    "model": "hey_listen"
  },
  "queue_depth": 0,
  "registry_size": 3
}
```

---

## Architecture

```
┌──────────────────────────────────────────┐
│  Menu Bar App (Python/rumps or SwiftUI)  │
│                                          │
│  Reads:                                  │
│  • sessions.json (active sessions)       │
│  • config.json (toggle states)           │
│                                          │
│  Polls:                                  │
│  • GET /status on each active session    │
│    (every 2s)                            │
│                                          │
│  Controls:                               │
│  • POST /set_voice, /stop, /mute, etc.  │
│  • Direct config.json writes (toggles)   │
│                                          │
│  Opens:                                  │
│  • IDE config files in default editor    │
│  • Re-renders voice rules template       │
└──────────────────────────────────────────┘
        │                │
        ▼                ▼
  sessions.json    MCP Server(s)
  config.json      HTTP :7865, :7866, ...
```

### Polling Strategy

- **Session list:** Read `sessions.json` every 5 seconds (file read, cheap)
- **Session status:** GET `/status` on each active session every 2 seconds (HTTP, lightweight)
- **Config values:** Read `config.json` on menu open (not polled — read on demand)
- **Voice list:** Static (54 voices from `shared.py` constants, bundled at build time)

### Process Lifecycle

- **Starts:** Automatically via LaunchAgent (same as audio-service), or manually from menu
- **Runs:** As a standalone process, independent of MCP server sessions
- **Survives:** Server restarts, session changes — always running
- **Quits:** Via "Quit VoiceSmith Menu" or manually killing the process

---

## Installation

### With `--with-menubar` flag

```bash
npx voicesmith-mcp install --with-menubar
```

Adds:
- Menu bar app to `~/.local/share/voicesmith-mcp/menubar/`
- LaunchAgent plist for auto-start: `com.voicesmith-mcp.menubar`
- `rumps` pip dependency (if Python implementation)

### Without flag

Menu bar app is not installed. All functionality remains available via MCP tools, config files, and CLI commands.

---

## Framework Decision

| Option | Pros | Cons |
|--------|------|------|
| **rumps (Python)** | Same language as server, can import shared.py directly, quick to build | Requires Python process running, ~30MB memory, less native feel |
| **SwiftUI (native)** | Native macOS look, tiny memory (~5MB), smooth animations | Separate language, can't share code with server, harder to maintain |
| **Electron** | Cross-platform potential | Massive overhead (~100MB), overkill |

**Recommendation:** Start with `rumps` (Python) for speed. Migrate to SwiftUI later if memory/polish matters. The HTTP API is the same either way.

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `menubar/app.py` | Create | Menu bar app (rumps-based) |
| `menubar/icons/` | Create | Menu bar icons (idle, active, recording, muted, error) |
| `server.py` | Modify | Extend `/status`, add `/set_voice`, `/stop`, `/mute`, `/unmute` HTTP endpoints |
| `config.py` | — | No changes needed (already has all config fields) |
| `bin/install.js` | Modify | `--with-menubar` flag, LaunchAgent setup |
| `bin/uninstall.js` | Modify | Remove menubar app and LaunchAgent |
| `install.sh` | Modify | Same additions for shell installer |
| `com.voicesmith-mcp.menubar.plist` | Create | LaunchAgent for auto-start |

---

## Security

- HTTP endpoints bind to `127.0.0.1` only — same as existing
- No authentication (localhost, same-user assumption)
- Config writes use atomic temp file + rename (existing pattern)
- Voice rules reset requires user confirmation dialog
- Menu bar app runs as the same user, same permissions

---

## Verification

1. Install with `--with-menubar` → menu bar icon appears
2. Start a session → icon changes from dim to active, session name shows
3. Toggle media ducking → config.json updates, next speak respects it
4. Switch voice via menu → session renames, AI uses new voice
5. Switch Whisper model → config updates, notification shown
6. View voice rules → opens correct file in editor
7. Reset voice rules → sentinel block replaced with fresh template
8. Stop playback → current speech stops
9. Test voice → plays sample phrase
10. Multiple sessions → both shown in session list
11. Kill a session → disappears from list within 5 seconds
12. Quit menu bar app → icon removed, sessions unaffected
13. Reboot → menu bar app auto-starts via LaunchAgent

---

## Future Enhancements

- **Dark mode icon variants** — auto-switch based on macOS appearance
- **Keyboard shortcut** — global hotkey to open the menu (e.g., Ctrl+Shift+V)
- **Notification center** — show notifications for session events (new session, voice switch, errors)
- **SwiftUI migration** — native implementation for better performance and polish
- **Volume slider** — control TTS output volume from the menu
- **Speech speed slider** — adjust `default_speed` from the menu
- **Linux tray support** — via `pystray` for Linux desktop environments
