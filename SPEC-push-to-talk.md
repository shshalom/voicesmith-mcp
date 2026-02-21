# Push-to-Talk & Multi-Session Voice — Design Spec

## Context

The Agent Voice MCP Server (SPEC.md) provides TTS and STT via MCP tools. Currently, the `listen` tool only activates when the AI calls it. This spec adds:

1. **User-initiated voice input (push-to-talk)** — The user says a universal wake phrase via macOS Voice Control. Their speech is transcribed, the target session is identified by name, and the text is submitted to the IDE.
2. **Multi-session support** — Multiple IDE sessions can run concurrently, each with a unique voice/name, without collisions.
3. **Cross-session audio coordination** — A global `flock`-based lock prevents overlapping speech playback across sessions.
4. **Listen timeout behavior** — When the AI asks a question and the user doesn't respond, speak a nudge and fall back to text.

---

## Push-to-Talk Architecture

### Flow

```
User says: "Hey listen"
        │
        ▼  (macOS Voice Control triggers Automator workflow)
  voice-input.sh starts
        │
        ▼  Plays a short beep (ready signal)
        │
        ▼  Picks any active session from sessions.json
        │  (or the only session if just one is active)
        │
        ▼  curl -X POST http://127.0.0.1:<port>/listen
┌──────────────────────────────────────┐
│  MCP Server (already running)         │
│                                       │
│  HTTP listener thread on :<port>      │
│  Whisper: already loaded ✓ (0ms)      │
│  VAD: already loaded ✓                │
│                                       │
│  → opens mic                          │
│  → records speech                     │
│  → VAD detects silence → stop         │
│  → Whisper transcribes                │
│  → returns { "text": "..." }          │
└──────────────────────────────────────┘
        │
        ▼  User said: "Eric, add error handling to the login"
  voice-input.sh parses response
        │
        ▼  First word matches a session name?
        │  YES → route to that session
        │  NO  → route to last-active / only session
        │
        ▼  osascript: keystroke the message + press Enter
  Text appears in the target IDE session and is submitted
```

### Universal Wake Phrase

**One phrase, one setup, all sessions.**

- Wake phrase: `"Hey listen"` — set up once in macOS Voice Control
- After the wake phrase triggers, the user speaks naturally: `"Eric, add error handling"`
- The script parses the first word to identify the target session name
- If only one session is active, the name is optional — any speech routes there
- If multiple sessions are active and no name matches, route to the most recently active session

**Why not per-name wake phrases:** macOS Voice Control commands must be set up manually in System Settings. One universal phrase = one-time setup. Per-name phrases would require re-configuring Voice Control every time a new session starts with a different name.

### Components

#### 1. HTTP Listener Thread in `server.py`

A lightweight HTTP server runs in a **daemon thread** alongside the stdio MCP transport. Uses Python's built-in `http.server` — no framework dependencies.

**Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| `POST /listen` | Record mic → transcribe → return JSON | Same logic as MCP `listen` tool |
| `GET /status` | Health check | Returns `{ "ready": true, "name": "Eric", "port": 7865 }` |

**Implementation details:**
- Binds to `127.0.0.1` only (localhost, not network-accessible)
- Port: base `7865`, incremented per active session (configurable via `config.json` `"http_port"` or env `VOICE_HTTP_PORT`)
- The `/listen` handler bridges to async via `asyncio.run_coroutine_threadsafe()` on the main event loop to reuse `_mic_capture.record()` and `_stt_engine.transcribe()`
- Reuses existing `_stt_engine`, `_vad`, `_mic_capture` instances (already loaded in memory — zero startup delay)
- Respects the same `_listen_active` lock — if the AI is already listening via MCP, HTTP `/listen` returns `{ "error": "mic_busy" }`
- Respects mute state
- Stores a reference to the asyncio event loop at startup for `run_coroutine_threadsafe()`

**Why a thread, not async:** The MCP server's event loop is owned by FastMCP's `run()`. A separate daemon thread with `http.server.HTTPServer` is simple, isolated, and doesn't interfere with the MCP protocol. The thread bridges to async only when handling `/listen` requests.

#### 2. `voice-input.sh` — Shell Script

Installed to `~/.local/share/agent-voice-mcp/voice-input.sh` (executable).

```bash
#!/bin/bash
# Voice Input — triggered by macOS Voice Control "Hey listen"
#
# 1. Read sessions.json to find active sessions
# 2. Pick a session (only one? use it. Multiple? record and parse name.)
# 3. curl the server's /listen endpoint
# 4. Parse the first word as session name (if multiple sessions)
# 5. Use osascript to type text into the frontmost app and press Enter

SESSIONS_FILE="$HOME/.local/share/agent-voice-mcp/sessions.json"

# ... check sessions, pick port, curl /listen, parse response ...
# ... osascript keystroke + return ...
```

**Session routing logic:**
1. Read `sessions.json`, filter out stale PIDs
2. If 0 active sessions → notification: "No active voice session"
3. If 1 active session → use it directly, full text is the message
4. If multiple sessions → record speech, parse first word against session names
   - Match found → route message (minus the name) to that session's port
   - No match → route to most recently started session

**Error handling:**
| Scenario | Behavior |
|----------|----------|
| No active sessions | macOS notification: "No active voice session. Start a coding session first." |
| Server not responding | macOS notification: "Voice server not responding" |
| No speech detected (timeout) | macOS notification: "No speech detected" |
| Mic busy (AI already listening) | macOS notification: "Mic is busy — the AI is already listening" |
| Transcription error | macOS notification with error message |

**macOS notifications** via `osascript -e 'display notification "..." with title "Agent Voice"'`

**Text injection** via AppleScript:
```applescript
tell application "System Events"
    keystroke theText
    keystroke return
end tell
```

**Note:** The user must grant Accessibility permission to the Automator workflow / shell script for `keystroke` to work. The installer should guide this.

#### 3. macOS Voice Control Setup

**One-time setup — the installer helps but can't fully automate:**

The installer:
1. Generates a `.voicecontrolcommands` plist file with the "Hey listen" phrase configured to run the Automator workflow
2. Creates the Automator workflow (`.workflow`) at `~/.local/share/agent-voice-mcp/VoiceInput.workflow` that runs `voice-input.sh`
3. Opens System Settings → Accessibility → Voice Control for the user to import the commands file
4. Displays clear instructions for the one manual step (clicking Import)

**Why not fully automated:** macOS Voice Control does not expose a programmatic API for importing custom commands. The `.voicecontrolcommands` file can be generated programmatically (it's an XML plist), but importing it requires the user to click "Import Custom Commands" in System Settings. This is a one-time action.

**Alternative manual setup:**
1. System Settings → Accessibility → Voice Control → enable
2. Commands → click + to add custom command
3. Phrase: "Hey listen"
4. Action: Run Automator Workflow → select `VoiceInput.workflow`
5. Application: Any Application

---

## Multi-Session Support

### Problem

Multiple IDE sessions may run simultaneously (e.g., two Claude Code terminals, or Claude Code + Cursor). Each spawns its own MCP server process. They must not collide on:
- HTTP port
- Voice name (each session has its own identity)
- Audio playback (speakers)
- Microphone access

### Solution: Session Registry

A shared JSON file at `~/.local/share/agent-voice-mcp/sessions.json` tracks active sessions:

```json
{
  "sessions": [
    {
      "name": "Eric",
      "voice": "am_eric",
      "port": 7865,
      "pid": 12345,
      "started_at": "2026-02-21T13:00:00Z"
    },
    {
      "name": "Nova",
      "voice": "af_nova",
      "port": 7866,
      "pid": 12346,
      "started_at": "2026-02-21T13:05:00Z"
    }
  ]
}
```

**File locking:** Access to `sessions.json` itself uses `flock` to prevent race conditions when multiple servers start simultaneously.

### Session Lifecycle

**On startup:**
1. Acquire `flock` on `sessions.json`
2. Read existing sessions
3. **Stale session cleanup:** For each registered session, check if the PID is alive (`os.kill(pid, 0)`). Remove dead entries.
4. Check if the configured main agent name is already taken by another active session
5. If taken → auto-assign the next available Kokoro voice name from the pool
6. Claim a port: find the lowest available port starting from base (7865)
7. Write this session's entry to `sessions.json`
8. Release `flock`
9. Start the HTTP listener on the claimed port
10. Log the assigned name and port so the user knows: `"Session started: Eric on port 7865"`

**On shutdown (graceful):**
1. Stop audio playback and recording
2. Save voice registry
3. Acquire `flock` on `sessions.json`
4. Remove this session's entry
5. Release `flock`
6. Exit

**On crash (ungraceful):**
- The PID-based stale check on next startup handles cleanup
- `flock` on the audio lock is released automatically by the OS
- `flock` on sessions.json is released automatically by the OS
- No stale state persists

### Voice Name Uniqueness

Each active session MUST have a unique voice name. This ensures:
- Name-based routing works unambiguously
- Each session speaks with a distinct voice
- No confusion about which session is talking

**Assignment order:**
1. Use the configured `main_agent` from `config.json` (e.g., "Eric")
2. If that name is already active in another session → auto-assign the next available Kokoro voice name
3. Log which name was assigned so the user knows

**The first session always gets the user's preferred name.** Second, third sessions get auto-assigned names.

### Port Assignment

- Base port: `7865` (configurable via `http_port` in config.json)
- Each session claims the lowest available port starting from base
- Port is written to `sessions.json` so `voice-input.sh` can look it up by name

---

## Cross-Session Audio Coordination

### Problem

Two sessions speaking simultaneously = garbled audio. Sub-agents within a session are already serialized by the SpeechQueue, but separate server processes have independent queues.

### Solution: `fcntl.flock` on a shared file

Before playing audio, acquire an exclusive lock on `/tmp/agent-voice-audio.lock`. Release after playback completes.

```python
# In audio_player.py play():
import fcntl

AUDIO_LOCK_PATH = "/tmp/agent-voice-audio.lock"

def play(self, samples, sample_rate):
    # ... write temp WAV ...
    with open(AUDIO_LOCK_PATH, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)  # Block until lock acquired
        # ... subprocess play ...
    # Lock auto-released when file handle closes
```

### Why `flock` and not a file-based flag

| Property | `flock` (new) | File flag (old talking stick) |
|----------|---------------|-------------------------------|
| Stale on crash | **No** — OS releases automatically | **Yes** — file persists |
| Blocking | Built-in — process sleeps until available | Must poll/retry |
| Cross-process | Yes | Yes |
| Performance | Kernel-level, near-zero overhead | File I/O on each check |
| Cleanup needed | None | Manual deletion / timeout |

### Crash Handling

`flock` is tied to the **file descriptor**, not the file itself:
- Process exits normally → FD closed → lock released
- Process crashes (SIGKILL, segfault, OOM) → OS closes all FDs → lock released
- Process hangs → other processes block until it completes or is killed
- Machine reboots → `/tmp` is cleared, lock file recreated fresh

**There is no stale lock scenario with `flock`.** The OS kernel guarantees cleanup.

### Edge case: Long speech blocking others

If Eric's session is playing a long (30-second) speech, Nova's session blocks for 30 seconds. This is intentional — serialized playback is better than overlapping audio. If this becomes a UX issue, a future enhancement could add a non-blocking try with skip/queue behavior.

---

## Listen Timeout Behavior

When the AI asks a question via `speak_then_listen` and the user doesn't respond:

| After timeout | Action |
|---------------|--------|
| **Speak a nudge** | The AI speaks: "I didn't catch that. Go ahead and type it." |
| **Fall back to text** | The AI waits for typed input. Does not retry `listen`. |
| **One nudge only** | Never repeat the nudge or re-open the mic automatically. |

For push-to-talk (user-initiated via "Hey listen"):
| After timeout | Action |
|---------------|--------|
| **macOS notification** | "No speech detected" |
| **No spoken response** | The AI didn't ask anything, so no voice nudge needed. |

---

## Configuration Changes

### `config.json` additions

```json
{
  "http_port": 7865
}
```

### `config.py` additions

```python
@dataclass
class AppConfig:
    # ... existing fields ...
    http_port: int = 7865
```

### `shared.py` additions

```python
DEFAULT_HTTP_PORT = 7865
SESSIONS_FILE_NAME = "sessions.json"
AUDIO_LOCK_PATH = "/tmp/agent-voice-audio.lock"
```

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `server.py` | Modify | HTTP listener thread, session registration/cleanup, event loop reference, port claiming |
| `tts/audio_player.py` | Modify | Add `flock`-based cross-session audio lock around playback |
| `config.py` | Modify | Add `http_port` field to AppConfig |
| `shared.py` | Modify | Add constants (DEFAULT_HTTP_PORT, SESSIONS_FILE_NAME, AUDIO_LOCK_PATH) |
| `config.json` | Modify | Add `http_port` default |
| `voice-input.sh` | Create | Shell script: read sessions → curl /listen → parse name → osascript paste |
| `bin/install.js` | Modify | Copy voice-input.sh, generate .voicecontrolcommands file, Voice Control setup guidance |
| `tests/test_server.py` | Modify | Add tests for HTTP listener, session registry, audio lock |

---

## Security

- HTTP binds to `127.0.0.1` only — not network-accessible
- No authentication (localhost, same-user assumption)
- `sessions.json` is user-readable only (standard umask)
- Audio lock at `/tmp/` uses standard POSIX permissions
- Microphone activation requires prior macOS permission grant
- `keystroke` via AppleScript requires Accessibility permission for the workflow

---

## Verification

1. **Single session:** Start Claude Code → `curl http://127.0.0.1:7865/status` → `{ "ready": true, "name": "Eric" }`
2. **Listen via HTTP:** `curl -X POST http://127.0.0.1:7865/listen` → speak → `{ "text": "..." }`
3. **voice-input.sh:** Run script → speak → text pasted into terminal
4. **Multi-session:** Start two sessions → `cat sessions.json` → two entries with different names/ports
5. **Name uniqueness:** Second session auto-assigns a different name → logged on startup
6. **Name routing:** With two sessions, say "Hey listen" → "Eric, do this" → routes to Eric's session
7. **Audio lock:** Two sessions speak simultaneously → audio is serialized, not overlapping
8. **Crash recovery:** `kill -9` a server → start new session → stale entry cleaned up, lock released
9. **Timeout nudge:** AI asks question → don't respond → AI speaks "I didn't catch that" → waits for text
10. **Voice Control:** Say "Hey listen" → speak → text submitted to Claude Code
11. **All existing 120 tests pass**

---

## Future Enhancements

- **Ready beep** — Play a short audio beep when the mic opens (after "Hey listen") so the user knows to start speaking
- **Visual indicator** — Menu bar icon or notification when listening is active
- **Configurable submit behavior** — Paste only (no Enter) for review before sending
- **Cross-platform** — Linux equivalent using custom hotkeys / speech-dispatcher
- **Session picker UI** — If multiple sessions active, show a quick picker overlay
- **Automatic Accessibility permission** — Guide or automate the permission grant for keystroke injection
