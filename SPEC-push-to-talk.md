# Push-to-Talk & Multi-Session Voice — Design Spec

## Context

The Agent Voice MCP Server (SPEC.md) provides TTS and STT via MCP tools. Currently, the `listen` tool only activates when the AI calls it. This spec adds:

1. **User-initiated voice input (push-to-talk)** — The user says a wake phrase via macOS Voice Control and their speech is transcribed and submitted to the active IDE session.
2. **Multi-session support** — Multiple IDE sessions can run concurrently, each with a unique voice/name, without collisions.
3. **Cross-session audio coordination** — A global lock prevents overlapping speech playback across sessions.

---

## Push-to-Talk Architecture

### Flow

```
User says: "Hey Eric listen"
        │
        ▼  (macOS Voice Control custom command)
  voice-input.sh
        │
        ▼  reads ~/.local/share/agent-voice-mcp/sessions.json
        │  finds Eric → port 7865
        │
        ▼  curl -X POST http://127.0.0.1:7865/listen
┌──────────────────────────────────────┐
│  MCP Server (Eric's session)          │
│                                       │
│  HTTP listener thread on :7865        │
│  Whisper: already loaded ✓ (0ms)      │
│  VAD: already loaded ✓                │
│                                       │
│  → opens mic                          │
│  → VAD detects speech + silence       │
│  → Whisper transcribes                │
│  → returns { "text": "..." }          │
└──────────────────────────────────────┘
        │
        ▼  JSON response
  voice-input.sh
        │
        ▼  osascript: type text + press Enter
  Text appears in the focused IDE and is submitted
```

### Components

#### 1. HTTP Listener Thread in `server.py`

A lightweight HTTP server runs in a **daemon thread** alongside the stdio MCP transport. Uses Python's built-in `http.server` — no framework dependencies.

**Endpoints:**
| Method | Path | Description |
|--------|------|-------------|
| `POST /listen` | Record mic → transcribe → return text | Same logic as MCP `listen` tool |
| `GET /status` | Health check | Returns `{ "ready": true, "name": "Eric", "session_id": "..." }` |

**Implementation details:**
- Binds to `127.0.0.1` only (localhost, not network-accessible)
- Port: `7865` by default (configurable via `config.json` `"http_port"` or env `VOICE_HTTP_PORT`)
- The `/listen` handler bridges to async via `asyncio.run_coroutine_threadsafe()` on the main event loop
- Reuses existing `_stt_engine`, `_vad`, `_mic_capture` instances (already loaded)
- Respects the same `_listen_active` lock — if the AI is already listening via MCP, HTTP `/listen` returns `{ "error": "mic_busy" }`
- Respects mute state

**Why a thread, not async:** The MCP server's event loop is owned by FastMCP's `run()`. A separate daemon thread with `http.server.HTTPServer` is simple, isolated, and doesn't interfere with the MCP protocol.

#### 2. `voice-input.sh` — Shell Script

Installed to `~/.local/share/agent-voice-mcp/voice-input.sh` (executable).

```bash
#!/bin/bash
# 1. Read sessions.json to find the right port for the wake name
# 2. curl the server's /listen endpoint
# 3. Extract text from JSON response
# 4. Use osascript to type text into the frontmost app and press Enter
```

**Error handling:**
| Scenario | Behavior |
|----------|----------|
| Server not running | macOS notification: "Voice server not active" |
| No speech detected (timeout) | macOS notification: "No speech detected" |
| Mic busy (AI already listening) | macOS notification: "Mic is busy" |
| Transcription error | macOS notification with error message |

**macOS notifications** via `osascript -e 'display notification "..." with title "Agent Voice"'`

**Text injection** via AppleScript:
```applescript
tell application "System Events"
    keystroke "<transcribed text>"
    keystroke return
end tell
```

#### 3. macOS Voice Control Setup

**Manual setup (documented in README):**
1. System Settings → Accessibility → Voice Control → enable
2. Commands → click + to add custom command
3. Phrase: "Hey Eric listen" (personalized per voice choice)
4. Action: Open Automator Workflow → select the installed workflow
5. Application: Any Application

**Automated setup (installer):**
The installer creates an Automator workflow (`.workflow`) at `~/.local/share/agent-voice-mcp/VoiceInput.workflow` that runs `voice-input.sh`. Optionally provides a `npx agent-voice-mcp voice-control` command that guides setup.

**Trigger phrase:** Personalized from the voice picker choice. Stored in `config.json` as `"voice_trigger_phrase"`. Examples:
- Eric → "Hey Eric listen"
- Nova → "Hey Nova listen"
- Fenrir → "Hey Fenrir listen"

---

## Multi-Session Support

### Problem

Multiple IDE sessions may run simultaneously (e.g., two Claude Code terminals, or Claude Code + Cursor). Each spawns its own MCP server process. They must not collide on:
- HTTP port
- Voice name / wake phrase
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

### Session Lifecycle

**On startup:**
1. Server reads `sessions.json`
2. **Stale session cleanup:** For each registered session, check if the PID is alive (`os.kill(pid, 0)`). If not, remove it.
3. Check if the configured main agent name is already taken by another active session
4. If taken → pick the next available voice name from the pool (auto-assign)
5. Claim a port: base port (7865) + number of active sessions
6. Write this session's entry to `sessions.json`
7. Start the HTTP listener on the claimed port

**On shutdown (graceful):**
1. Stop audio playback
2. Save voice registry
3. Remove this session's entry from `sessions.json`
4. Exit

**On crash (ungraceful):**
- The PID-based stale check on next startup handles this
- `flock` on the audio lock is released automatically by the OS
- No stale state persists

### Voice Name Uniqueness

Each active session MUST have a unique voice name. This ensures:
- "Hey Eric listen" routes to exactly one session
- No two sessions share a wake phrase
- Each session speaks with a distinct voice

**Assignment order:**
1. Use the configured `main_agent` from `config.json` (e.g., "Eric")
2. If that name is already active in another session, auto-assign the next available Kokoro voice name
3. Log which name was assigned so the user knows the wake phrase

**The first session always gets the user's preferred name.** Second, third sessions get auto-assigned names.

### Port Assignment

- Base port: `7865` (configurable)
- Each session gets `base_port + session_index`
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

| Property | `flock` | File flag (old talking stick) |
|----------|---------|-------------------------------|
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

If Eric's session is playing a long (30-second) speech, Nova's session blocks for 30 seconds. This is intentional — serialized playback is better than overlapping audio. If this becomes a UX issue, a future enhancement could add a timeout:

```python
fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Non-blocking
# If would block, queue or skip
```

But for v1, blocking is the correct behavior.

---

## Configuration Changes

### `config.json` additions

```json
{
  "http_port": 7865,
  "voice_trigger_phrase": "Hey Eric listen"
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
SESSIONS_FILE = "sessions.json"
AUDIO_LOCK_PATH = "/tmp/agent-voice-audio.lock"
```

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `server.py` | Modify | HTTP listener thread, session registration/cleanup, port assignment |
| `audio_player.py` | Modify | Add `flock`-based cross-session audio lock |
| `config.py` | Modify | Add `http_port` field |
| `shared.py` | Modify | Add constants (port, session file, lock path) |
| `config.json` | Modify | Add `http_port`, `voice_trigger_phrase` defaults |
| `voice-input.sh` | Create | Shell script: read sessions.json → curl → osascript paste |
| `bin/install.js` | Modify | Copy voice-input.sh, add Voice Control setup guidance |
| `tests/test_server.py` | Modify | Tests for HTTP listener, session registry |

---

## Security

- HTTP binds to `127.0.0.1` only — not network-accessible
- No authentication (localhost, same-user assumption)
- `sessions.json` is user-readable only (standard umask)
- Audio lock at `/tmp/` uses standard POSIX permissions
- Microphone activation requires prior macOS permission grant

---

## Verification

1. **Single session:** Start Claude Code → `curl http://127.0.0.1:7865/status` → `{ "ready": true, "name": "Eric" }`
2. **Listen via HTTP:** `curl -X POST http://127.0.0.1:7865/listen` → speak → `{ "text": "..." }`
3. **voice-input.sh:** Run script → speak → text pasted into terminal
4. **Multi-session:** Start two sessions → `cat sessions.json` → two entries with different names/ports
5. **Name uniqueness:** Second session auto-assigns a different name → logged on startup
6. **Audio lock:** Two sessions speak simultaneously → audio is serialized, not overlapping
7. **Crash recovery:** Kill -9 a server → start new session → stale entry cleaned up, lock released
8. **Voice Control:** Say "Hey Eric listen" → speak → text submitted to Claude Code
9. **All existing tests pass**

---

## Future Enhancements

- **Automatic Voice Control command creation** via AppleScript/Shortcuts automation
- **Visual indicator** when listening (menu bar icon, notification)
- **Configurable submit behavior** — paste only (no Enter) for review before sending
- **Cross-platform** — Linux equivalent using custom hotkeys / speech-dispatcher
- **Session picker UI** — if multiple sessions active, show a quick picker
