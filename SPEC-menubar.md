# Menu Bar App — Design Spec

## Context

VoiceSmith MCP has grown beyond basic TTS/STT. It now includes media ducking, wake word detection, configurable nudge, multiple mic backends, and multi-session coordination. Users need a way to see what's happening and control settings without editing config files or asking the AI. A native macOS menu bar app provides always-visible status and one-click controls.

## Overview

- **Platform:** macOS only (matches VoiceSmithMCP.app native launcher)
- **Framework:** `rumps` (Python, lightweight menu bar apps) or SwiftUI (native)
- **Communication:** HTTP polling against each session's `/status` endpoint + config mutations via `POST /config`
- **Install:** Optional — bundled with the installer, enabled via `--with-menubar` flag

---

## Menu Bar Icon

The icon reflects the current mic/voice state at a glance. **All icons are static PNGs** (6 variants required — `rumps` does not support animation):

| State | Icon | Description |
|-------|------|-------------|
| Idle (no sessions) | 🎙 dim | No active VoiceSmith sessions |
| Active (listening for wake word) | 🎙 normal | Wake word listener is monitoring |
| Recording (speech capture) | 🎙 red dot | User is speaking, mic is recording (static red dot overlay, not animated) |
| AI listening (speak_then_listen) | 🎙 blue | AI asked a question, waiting for response |
| Muted | 🎙 crossed | All voice output silenced |
| Error | 🎙 red | TTS or STT failed to load |

The icon updates by polling active sessions. Poll interval: **2 seconds while menu is open, 10 seconds while closed** (reduces unnecessary load).

If animated pulsing is desired in the future, this is a reason to migrate to SwiftUI (see Framework Decision).

---

## Session Selection Model

When multiple sessions are active, the menu bar needs to know which session to target for actions like Stop, Test Voice, and Set Voice.

**Model: Click-to-select with smart default.**

- The **most recently registered session** is auto-selected as the target on startup.
- Clicking a session in the session list **selects it as the active target**. A checkmark (✓) indicates the selected session.
- All single-session actions (Stop, Test Voice, Set Voice, Server Health) target the selected session.
- **Mute targets ALL sessions** — when you need silence, you need it from everyone. Per-session mute is available via right-click context menu on individual sessions.
- If the selected session dies, auto-select the next most recently registered session. If no sessions remain, show the idle icon.

---

## Menu Structure

```
┌─────────────────────────────────────────┐
│  VoiceSmith MCP                    v1.x │
│─────────────────────────────────────────│
│  ✓ Fenrir (am_fenrir)        port 7865  │  ← selected (click to select)
│    Nova (af_nova)            port 7866  │  ← click to select
│─────────────────────────────────────────│
│  🔇 Mute All                            │  ← mutes ALL sessions
│─────────────────────────────────────────│
│  ✓ Media Ducking                        │  ← toggle (config write via HTTP)
│    Nudge on Timeout                     │  ← toggle (config write via HTTP)
│  ✓ Wake Word                            │  ← toggle (HTTP + config)
│─────────────────────────────────────────│
│  Voice ►                                │  ← nested submenu
│  │  American English ►                  │
│  │  │  ✓ Fenrir (am_fenrir)            │
│  │  │    Eric (am_eric)                 │
│  │  │    Adam (am_adam)                 │
│  │  │    Nova (af_nova)                 │
│  │  │    ... (20 voices)               │
│  │  British English ►                   │
│  │  │    Daniel (bm_daniel)             │
│  │  │    Alice (bf_alice)               │
│  │  │    ... (8 voices)                │
│  │  Other Languages ►                   │
│  │  │  Spanish ►                        │
│  │  │  French ►                         │
│  │  │  ... (26 voices)                 │
│─────────────────────────────────────────│
│  Whisper Model ►                        │  ← submenu
│  │  ✓ base (~150MB, fastest)           │
│  │    small (~500MB, better accuracy)  │
│  │    medium (~1.5GB, very accurate)   │
│  │    large-v3 (~3GB, best accuracy)   │
│─────────────────────────────────────────│
│  Voice Rules ►                          │  ← submenu
│  │  Edit Rules...                       │  ← opens in default editor
│  │  Preview Rules (Quick Look)          │  ← qlmanage -p (non-editable preview)
│  │  Reset to Default                    │  ← re-renders from template
│─────────────────────────────────────────│
│  Server Health ►                        │  ← submenu (selected session)
│  │  TTS: ● loaded (kokoro-v1.0)       │
│  │  STT: ● loaded (whisper-base)      │
│  │  VAD: ● loaded                      │
│  │  Uptime: 1h 23m                     │
│  │  Queue depth: 0                     │
│─────────────────────────────────────────│
│  Stop Playback                          │  ← action (selected session)
│  Test Voice                             │  ← action (selected session)
│  Open Config...                         │  ← opens config.json in editor
│─────────────────────────────────────────│
│  ⬆ Update Available (v1.0.19)          │  ← shown only when update exists
│  Release Notes...                       │  ← opens GitHub releases
│  Version: v1.0.18                       │  ← current version (dimmed)
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
- Checkmark (✓) for the currently selected target session
- Clicking a session **selects it as the target** for all single-session actions

**Data source:** Read `sessions.json` with `fcntl.flock(LOCK_SH)` (shared read lock) to avoid reading partial writes during server updates.

**Note on file safety:** The server's `_write_sessions()` truncates the file before writing. A lockless read could see empty or partial JSON. The shared read lock (`LOCK_SH`) cooperates with the server's exclusive write lock (`LOCK_EX`) — the reader blocks only while a write is in progress.

**Prerequisite:** The server's `_write_sessions()` should be migrated to atomic writes (temp file + `os.rename`) as a future improvement. This would make all readers safe without any locking. Until then, `LOCK_SH` is required.

### 2. Mute

**Mute All** is the top-level mute action — it calls `POST /mute` on ALL active sessions. This is the "silence everything right now" button for meetings.

Per-session mute is available via right-click context menu on individual sessions in the session list.

**Unmute:** When muted, the menu item changes to "🔊 Unmute All" and calls `POST /unmute` on all sessions.

### 3. Quick Toggles

One-click toggles that update `config.json` via `POST /config` on the selected session:

| Toggle | Config key | Effect |
|--------|-----------|--------|
| Media Ducking | `tts.duck_media` | Auto-pause music during speech |
| Nudge on Timeout | `stt.nudge_on_timeout` | Speak nudge when listen times out |
| Wake Word | `wake_word.enabled` | Enable/disable wake word listener |

**How toggles work:**
1. Call `POST /config` on the selected session with `{"key": "tts.duck_media", "value": false}`
2. The server updates `config.json` atomically (single writer, no race conditions)
3. For wake word: the server also starts/stops the wake listener thread after updating config

**Why route config writes through HTTP:** The server, voice registry, and menu bar app would otherwise all write to `config.json` without coordination. By routing all mutations through a single `POST /config` endpoint, the server becomes the sole writer, eliminating race conditions.

### 4. Voice Switcher

Nested submenu listing all 54 Kokoro voices, grouped by language:

```
Voice >
  American English >
    ✓ Fenrir (am_fenrir)
      Eric (am_eric)
      Adam (am_adam)
      Nova (af_nova)
      ... (20 voices)
  British English >
      Daniel (bm_daniel)
      Alice (bf_alice)
      ... (8 voices)
  Other Languages >
    Spanish >
      ...
    French >
      ...
```

Current voice has a checkmark. Clicking a voice:
1. Calls `POST /set_voice` on the selected session with `{"voice": "am_fenrir"}`
2. The server derives the name from the voice ID (e.g., `am_fenrir` → "Fenrir") and renames the session
3. Menu updates to show new name

**Requires:** New HTTP endpoint `POST /set_voice` on the server. Body: `{"voice": "am_fenrir"}` — the server derives the name from the voice ID (same logic as the MCP tool at `server.py` lines 638-641). The `name` parameter is not required in the HTTP body.

### 5. Whisper Model Switcher

Submenu showing available model sizes with current selection checkmarked:

| Model | Size | Speed | When to use |
|-------|------|-------|-------------|
| base | ~150MB | ~0.2s | Default — fast, good for clear speech |
| small | ~500MB | ~0.5s | Accented speech, noisy environments |
| medium | ~1.5GB | ~1.5s | Complex sentences, multiple languages |
| large-v3 | ~3GB | ~3s | Maximum accuracy |

Clicking a model:
1. Calls `POST /config` on the selected session with `{"key": "stt.model_size", "value": "small"}`
2. Shows a notification: "Whisper model changed to 'small'. Close and reopen your IDE session to apply."
3. The model downloads automatically on next session startup (faster-whisper handles this)

**Note:** Model switch requires server restart because faster-whisper loads the model once at startup. The menu bar app cannot hot-swap models.

### 6. Voice Rules

| Action | What it does |
|--------|-------------|
| **Edit Rules...** | Opens the installed voice rules file in the default text editor. Path depends on IDE: `~/.claude/CLAUDE.md` for Claude Code, `~/.cursor/rules/voicesmith.mdc` for Cursor. |
| **Preview Rules (Quick Look)** | Shows a non-editable Quick Look preview via `qlmanage -p <path>`. Fulfills "view" intent without opening an editor. |
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

### 7. Server Health

Read-only submenu showing component status. Polled from the **selected** session's HTTP `/status` endpoint.

| Item | Source | Display |
|------|--------|---------|
| TTS | `tts.loaded` | Green/red dot + model name |
| STT | `stt.loaded` | Green/red dot + model name |
| VAD | `vad.loaded` | Green/red dot |
| Uptime | `started_at` | Human-readable (e.g., "1h 23m") |
| Queue depth | `queue_depth` | Number of pending speech items |
| Wake word state | `wake_word.state` | listening / recording / yielded / disabled |

### 8. Actions

| Action | What it does |
|--------|-------------|
| **Stop Playback** | Calls `POST /stop` on the selected session — stops any playing audio and cancels active listen |
| **Test Voice** | Calls `POST /speak` on the selected session with `{"text": "VoiceSmith is working.", "name": "<session_name>", "block": false}`. Uses `block: false` so the menu remains responsive during playback. |
| **Open Config...** | Opens `~/.local/share/voicesmith-mcp/config.json` in the default text editor |

### 9. Updates

Check for new versions and update in place:

| Item | Display |
|------|---------|
| **Current version** | e.g., "v1.0.18" (read from `package.json` in install dir) |
| **Update available** | Badge on menu bar icon + "Update Available (v1.0.19)" menu item |
| **Update now** | One-click update — runs `npx voicesmith-mcp@<version> install --update` |
| **Release notes** | Opens GitHub releases page in browser |

**How version check works:**
1. On app launch and every 6 hours, make a direct HTTPS request to `https://registry.npmjs.org/voicesmith-mcp/latest` (5-second timeout)
2. Parse the `version` field from the JSON response
3. Compare against installed version from `~/.local/share/voicesmith-mcp/package.json`
4. If newer version exists, show notification and badge the menu bar icon
5. Cache the check result to avoid repeated requests

**Why not `npm view`:** Requires npm on PATH (not guaranteed for git-clone installs), can hang behind corporate proxies, writes warnings to stderr. A direct HTTPS request is simpler, faster, and has no dependencies.

**Disable update checks:** Set `"check_updates": false` in `config.json` for air-gapped networks.

**How update works:**
1. User clicks "Update Now"
2. Show confirmation: "Update VoiceSmith MCP from v1.0.18 to v1.0.19? Active sessions will restart."
3. Run `npx voicesmith-mcp@1.0.19 install --update` in a subprocess (version pinned to match what was shown to the user)
4. The installer is already idempotent — it preserves config, merges new defaults, updates voice rules
5. Show progress notification: "Updating..." → "Updated to v1.0.19. Restart your sessions."
6. Active sessions need manual restart (or the installer could signal them via SIGTERM)

**Offline:** If the registry is unreachable, skip the check silently. No error shown — the menu just doesn't show an update badge.

---

## HTTP API Changes Required

The current HTTP API is minimal (GET `/status`, POST `/listen`, `/speak`, `/session`). The menu bar app needs:

| Endpoint | Method | Body | Purpose |
|----------|--------|------|---------|
| `/status` | GET | — | **Extend** to include full status (TTS/STT/VAD loaded, muted, wake state, config values, full session_info) |
| `/config` | POST | `{"key": "tts.duck_media", "value": false}` | **New** — single writer for config.json. Server validates, saves atomically, and applies side effects (e.g., start/stop wake listener). |
| `/set_voice` | POST | `{"voice": "am_fenrir"}` | **New** — mirrors MCP `set_voice` tool. Name derived from voice ID by the server. |
| `/stop` | POST | — | **New** — mirrors MCP `stop` tool |
| `/mute` | POST | — | **New** — mirrors MCP `mute` tool |
| `/unmute` | POST | — | **New** — mirrors MCP `unmute` tool |
| `/wake_enable` | POST | — | **New** — HTTP binding for MCP `wake_enable` tool |
| `/wake_disable` | POST | — | **New** — HTTP binding for MCP `wake_disable` tool |
| `/speak` | POST | `{"name": "...", "text": "...", "block": false}` | **Existing** — used by Test Voice action. Accept `block` field. |
| `/listen` | POST | — | **Existing** — used by push-to-talk (no menu bar changes needed) |

The new endpoints bridge HTTP to the same async functions the MCP tools call, using `asyncio.run_coroutine_threadsafe()` (same pattern as the existing `/listen` handler).

**Threading:** Switch the server from `HTTPServer` to `ThreadingHTTPServer` (one-line change). This allows concurrent request handling so `/status` polls are not blocked by an active `/listen` call (which can block for up to 30 seconds).

### Extended `/status` Response

```json
{
  "ready": true,
  "name": "Fenrir",
  "voice": "am_fenrir",
  "port": 7865,
  "pid": 12345,
  "session_id": "abc-123",
  "tmux_session": "agent-voice-12345",
  "started_at": "2026-03-05T10:00:00Z",
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

**Backward compatibility:** The menu bar MUST treat ALL fields as optional and use `.get()` with sensible defaults. Servers running older versions may return a subset of these fields. Display "unknown" or a neutral state for any missing field.

---

## Architecture

```
┌──────────────────────────────────────────┐
│  Menu Bar App (Python/rumps or SwiftUI)  │
│                                          │
│  Reads:                                  │
│  • sessions.json (with LOCK_SH)         │
│                                          │
│  Polls:                                  │
│  • GET /status on each active session    │
│    (2s while menu open, 10s while closed)│
│                                          │
│  Controls:                               │
│  • POST /config (all config mutations)   │
│  • POST /set_voice, /stop, /mute, etc.  │
│                                          │
│  Opens:                                  │
│  • IDE config files in default editor    │
│  • config.json in default editor         │
│  • Re-renders voice rules template       │
└──────────────────────────────────────────┘
        │                │
        ▼                ▼
  sessions.json    MCP Server(s)
  (LOCK_SH read)   HTTP :7865, :7866, ...
                   (ThreadingHTTPServer)
```

### Polling Strategy

- **Session list:** Read `sessions.json` every 5 seconds (file read with `LOCK_SH`, cheap)
- **Session status:** GET `/status` on each active session (2s when menu open, 10s when closed). **1-second HTTP timeout** per request to prevent stalls from blocked servers.
- **Config values:** Read from `/status` response (included in extended schema) — no separate config.json reads needed
- **Voice list:** Static (54 voices bundled at build time from `shared.py` constants)

### Process Lifecycle

- **Starts:** Automatically via LaunchAgent (same as audio-service), or manually from menu
- **Runs:** As a standalone process, independent of MCP server sessions
- **Survives:** Server restarts, session changes — always running
- **Crash recovery:** LaunchAgent plist includes `KeepAlive: true` and `ThrottleInterval: 10` — launchd auto-restarts after crash, with 10-second cooldown to prevent rapid restart loops
- **Quits:** Via "Quit VoiceSmith Menu" or manually killing the process

---

## Installation

### With `--with-menubar` flag

```bash
npx voicesmith-mcp install --with-menubar
```

Adds:
- Menu bar app to `~/.local/share/voicesmith-mcp/menubar/`
- LaunchAgent plist for auto-start: `com.voicesmith-mcp.menubar` (with `KeepAlive` and `ThrottleInterval`)
- `rumps` pip dependency (if Python implementation)

### Without flag

Menu bar app is not installed. All functionality remains available via MCP tools, config files, and CLI commands.

---

## Framework Decision

| Option | Pros | Cons |
|--------|------|------|
| **rumps (Python)** | Same language as server, can import shared.py directly, quick to build | Requires Python process running, ~30MB memory, no icon animation, less native feel |
| **SwiftUI (native)** | Native macOS look, tiny memory (~5MB), smooth animations, Core Animation for pulsing icons | Separate language, can't share code with server, harder to maintain |
| **Electron** | Cross-platform potential | Massive overhead (~100MB), overkill |

**Recommendation:** Start with `rumps` (Python) for speed. The HTTP API is the same either way, making a future SwiftUI migration straightforward. If animated icons or native polish become requirements, migrate to SwiftUI.

**rumps limitations to be aware of:**
- No icon animation (must use static PNGs for all states)
- No section headers in submenus (use nested submenus for voice grouping)
- Icon updates must happen on the main thread (use `rumps.Timer` for polling, not raw threads)

---

## Server Prerequisites

These server changes are required **before** the menu bar app can function correctly:

| Change | File | Why |
|--------|------|-----|
| Switch to `ThreadingHTTPServer` | `server.py` | Prevent `/status` polls from blocking behind active `/listen` calls |
| Add `POST /config` endpoint | `server.py` | Single writer for config.json — eliminates triple-writer race |
| Add HTTP bindings for `/set_voice`, `/stop`, `/mute`, `/unmute`, `/wake_enable`, `/wake_disable` | `server.py` | Menu bar needs HTTP access to MCP tool functionality |
| Extend `GET /status` response | `server.py` | Include full session_info, config values, component health |
| Accept `block` field in `POST /speak` | `server.py` | Test Voice needs non-blocking mode |
| Fix `_start_periodic_save_thread` | `server.py` | Thread is defined but never started — stale sessions won't auto-clean without it |
| Add `check_updates` config field | `config.py`, `config.json` | Allow users to disable update checks |

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `menubar/app.py` | Create | Menu bar app (rumps-based) |
| `menubar/__init__.py` | Create | Python package init |
| `menubar/icons/` | Create | 6 static PNG icon variants (dim, normal, recording, blue, crossed, red) |
| `server.py` | Modify | Switch to `ThreadingHTTPServer`, extend `/status`, add `/config`, `/set_voice`, `/stop`, `/mute`, `/unmute`, `/wake_enable`, `/wake_disable` HTTP endpoints, accept `block` in `/speak`, fix `_start_periodic_save_thread` |
| `config.py` | Modify | Add `check_updates` field |
| `config.json` | Modify | Add `check_updates: true` default |
| `shared.py` | Reference | Voice list constants (`ALL_VOICE_IDS`, `VOICE_NAME_MAP`) — imported by menu bar app |
| `session_registry.py` | Reference | Menu bar reads `sessions.json` with `LOCK_SH` — uses same file format |
| `bin/install.js` | Modify | `--with-menubar` flag, LaunchAgent setup |
| `bin/uninstall.js` | Modify | Remove menubar app and LaunchAgent |
| `install.sh` | Modify | Same additions for shell installer |
| `com.voicesmith-mcp.menubar.plist` | Create | LaunchAgent for auto-start (with `KeepAlive`, `ThrottleInterval`) |

---

## Security

- HTTP endpoints bind to `127.0.0.1` only — same as existing
- No authentication (localhost, same-user assumption)
- Config writes routed through `POST /config` (server is sole writer)
- Voice rules reset requires user confirmation dialog
- Menu bar app runs as the same user, same permissions
- Update checks go to `registry.npmjs.org` over HTTPS — can be disabled via `check_updates: false`

---

## Verification

1. Install with `--with-menubar` → menu bar icon appears
2. Start a session → icon changes from dim to active, session name shows
3. Click a session → checkmark moves, actions target that session
4. Mute All → all sessions muted, icon changes to crossed
5. Unmute All → all sessions unmuted
6. Toggle media ducking → config.json updates via HTTP, next speak respects it
7. Switch voice via menu → session renames, AI uses new voice
8. Switch Whisper model → config updates via HTTP, notification shown
9. Edit voice rules → opens correct file in editor
10. Preview voice rules → Quick Look preview, non-editable
11. Reset voice rules → sentinel block replaced with fresh template (after confirmation)
12. Open Config → opens config.json in editor
13. Stop playback → current speech stops on selected session
14. Test voice → plays sample phrase on selected session (menu stays responsive)
15. Multiple sessions → both shown in session list, click to select
16. Kill a session → disappears from list within 5 seconds
17. Quit menu bar app → icon removed, sessions unaffected
18. Crash menu bar app → auto-restarts via LaunchAgent within 10 seconds
19. Reboot → menu bar app auto-starts via LaunchAgent
20. Version check → shows current version in menu
21. Newer version on npm → "Update Available" badge + menu item
22. Click "Update Now" → runs pinned-version installer, shows progress, completes
23. Offline → no update badge, no error
24. Old server (pre-extended `/status`) → menu shows "unknown" for missing fields, no crash

---

## Future Enhancements

- **Dark mode icon variants** — auto-switch based on macOS appearance
- **Keyboard shortcut** — global hotkey to open the menu (e.g., Ctrl+Shift+V)
- **Notification center** — show notifications for session events (new session, voice switch, errors)
- **SwiftUI migration** — native implementation for better performance, polish, and animated icons
- **Server-sent events (SSE)** — replace polling with `GET /events` push stream for real-time state updates
- **Volume slider** — control TTS output volume from the menu
- **Speech speed slider** — adjust `default_speed` from the menu
- **Restart session** — send SIGTERM to server PID, IDE respawns automatically
- **Atomic `_write_sessions`** — migrate to temp file + `os.rename` so readers don't need `LOCK_SH`
- **Linux tray support** — via `pystray` for Linux desktop environments
