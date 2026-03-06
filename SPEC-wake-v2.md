# Wake Word v2 — Menu Bar App Architecture

## Context

The original wake word spec (SPEC-wake-word.md) runs wake detection as a thread inside each MCP server process, using tmux for text injection. This creates mic ownership conflicts between sessions, requires tmux wrappers, and dies when sessions die.

This v2 spec moves wake word detection to the VoiceSmith menu bar app (VoiceSmith.app), which is always running, knows all sessions, and owns the mic independently.

### What Changes from v1

| Aspect | v1 (MCP server thread) | v2 (Menu bar app) |
|--------|----------------------|-------------------|
| **Where it runs** | Per-session thread in server.py | VoiceSmith.app (always running) |
| **Mic ownership** | Each server competes for mic | One app, one mic, no conflicts |
| **Session awareness** | Only knows its own session | Knows all sessions via sessions.json |
| **Text injection** | tmux send-keys | tmux send-keys (primary) + AppleScript (GUI IDEs) |
| **Survives session death** | No — dies with server | Yes — LaunchAgent keeps it alive |
| **Requires tmux** | Always | Only for terminal IDEs (Claude Code, Codex) |
| **Visual feedback** | None (menu bar was future work) | Orange icon indicator, already built |

### What Stays the Same

- **Wake phrase:** "Hey listen" (openWakeWord ONNX model)
- **Audio format:** 16kHz mono (int16 for wake detection, float32 for recording)
- **Transcription:** faster-whisper via session's HTTP `/listen` endpoint
- **Multi-session routing:** Parse first word as session name
- **Config:** `wake_word` section in config.json
- **Shell alias + tmux:** Still needed for terminal-based text injection

---

## Architecture

```
User says: "Hey listen... add error handling"
        │
        ▼
┌──────────────────────────────────────────────┐
│  VoiceSmith.app (menu bar, always running)    │
│                                               │
│  Wake Word Listener                           │
│  ├── Connects to audio-service Unix socket    │
│  │   (/tmp/voicesmith-audio.sock)             │
│  │   Resamples float32→int16 for wake model   │
│  ├── openWakeWord detects "Hey listen"        │
│  ├── Disconnects from socket (yields mic)     │
│  ├── Plays Tink ready sound                   │
│  ├── Reconnects to socket for recording       │
│  ├── VAD monitors for speech + silence        │
│  ├── Sends audio to session's /listen or      │
│  │   transcribes via Python subprocess         │
│  ├── Parses session name from transcription   │
│  ├── Injects text via tmux or AppleScript     │
│  └── Reconnects for wake word listening       │
│                                               │
│  Menu Bar (existing)                          │
│  ├── Icon goes orange during recording        │
│  ├── Wake toggle in Settings section          │
│  └── Shows listening state per session         │
└──────────────────────────────────────────────┘
        │                           │
        ▼                           ▼
  audio-service              MCP Server(s)
  (CoreAudio socket)         HTTP :7865, :7866...
```

### Why Use the Audio-Service Socket

The `audio-service` LaunchAgent already:
- Runs under launchd (proper TCC mic attribution)
- Streams CoreAudio float32 via Unix socket at `/tmp/voicesmith-audio.sock`
- Accepts multiple connections (loops on `accept()`)
- Handles one client at a time (new connection replaces old)

The menu bar app connects as a client. When the MCP server needs the mic for AI `listen`, it connects too — the audio-service disconnects the old client (menu bar) and streams to the new one (server). When the server disconnects, the menu bar reconnects.

**This solves mic ownership naturally.** No flags, no handoff protocol — the socket handles it.

---

## Wake Word Detection

### Engine: openWakeWord via Python Subprocess

The menu bar app is native Swift, but openWakeWord is Python. Options:

| Approach | Pros | Cons |
|----------|------|------|
| **Python subprocess (recommended)** | Reuses existing venv + model, minimal code | Spawns a long-running Python process (~20MB) |
| **ONNX Runtime Swift** | Native, no Python | Must reimplement audio preprocessing (mel spectrogram), complex |
| **macOS Speech Recognition** | No model needed | Can't do custom wake words, requires internet |

**Recommended: Python subprocess.** Launch a persistent Python process that reads from the audio socket and outputs wake events via stdout. The menu bar app reads its stdout for wake detections.

### Wake Detector Process

The menu bar app spawns:
```bash
~/.local/share/voicesmith-mcp/.venv/bin/python3 -c "
from wake_detector import run
run(socket_path='/tmp/voicesmith-audio.sock', model='hey_listen', threshold=0.5)
"
```

`wake_detector.py` (new file):
- Connects to the audio-service Unix socket
- Reads float32 chunks, resamples to int16 for openWakeWord
- Runs openWakeWord inference on 80ms frames
- On detection: prints `WAKE\n` to stdout
- On disconnect (mic yielded to AI): prints `YIELDED\n`, waits, reconnects
- On reconnect: prints `RESUMED\n`

The menu bar app reads these events and drives the state machine.

### State Machine

```
┌──────────┐    app starts       ┌───────────┐
│ DISABLED │ ─────────────────── │  STARTING  │
│          │  (feature off)      │ (spawning  │
└──────────┘                     │  detector) │
                                 └─────┬─────┘
                                       │ process ready
                                       ▼
                                 ┌───────────┐
                             ┌── │ LISTENING  │ ◄────────────────────┐
                             │   │ (wake word)│                      │
                             │   └─────┬─────┘                      │
                             │         │ WAKE event                  │
                             │         ▼                             │
                             │   ┌───────────┐                      │
                             │   │ RECORDING  │                      │
                             │   │ (speech)   │ timeout: 10s max     │
                             │   └─────┬─────┘                      │
                             │         │ silence / timeout           │
                             │         ▼                             │
                             │   ┌───────────┐                      │
                             │   │TRANSCRIBING│                      │
                             │   │            │                      │
                             │   └─────┬─────┘                      │
                             │         │ text ready                  │
                             │         ▼                             │
                             │   ┌───────────┐                      │
                             │   │ INJECTING  │ ─────────────────────┘
                             │   │ (tmux/AS)  │    resume listening
                             │   └───────────┘
                             │
                             │  AI calls listen (server takes socket)
                             │         │
                             │         ▼
                             │   ┌───────────┐
                             └── │ YIELDED    │ ── RESUMED event ───┐
                                 │ (mic given │                     │
                                 │  to AI)    │                     │
                                 └───────────┘                     │
                                       ▲                           │
                                       └───────────────────────────┘
```

### Icon States During Wake

| State | Menu Bar Icon | Description |
|-------|--------------|-------------|
| DISABLED | Normal (mic + dot) | Wake word off |
| LISTENING | Normal (mic + green dot) | Monitoring for wake phrase |
| RECORDING | Orange capsule | User is speaking after wake |
| TRANSCRIBING | Orange capsule (pulsing if possible) | Processing speech |
| INJECTING | Normal (mic + green dot) | Text sent, resuming |
| YIELDED | Normal (mic + blue dot?) | AI is listening, wake paused |

---

## Recording After Wake

When `WAKE` is detected:

1. **Disconnect from socket** — releases mic for clean audio
2. **Play Tink ready sound** — user knows to speak
3. **Reconnect to socket** — start capturing fresh audio
4. **Buffer audio chunks** — accumulate float32 frames
5. **Run VAD** — detect speech onset and silence
6. **On 1.5s silence or 10s max** — stop recording
7. **Transcribe** — send audio to session's `/listen` HTTP endpoint, or run Whisper locally via Python subprocess

### Transcription Options

| Method | How | When to use |
|--------|-----|-------------|
| **Session HTTP `/listen`** | POST audio data to active session's port | Session is running, fastest |
| **Python subprocess** | Run faster-whisper directly | No active sessions, or session's /listen is busy |
| **Whisper in Swift** | whisper.cpp via C bridge | Future, fully native |

**Recommended for v1:** Use the session's `/listen` endpoint. But `/listen` opens its own mic — we need a variant that accepts pre-recorded audio instead.

**New endpoint: `POST /transcribe`**
- Accepts raw audio data (float32, 16kHz mono) in the request body
- Runs faster-whisper on the provided audio (no mic)
- Returns `{"text": "...", "confidence": 0.95}`
- Reuses the existing `WhisperEngine` instance

This keeps the menu bar app thin — it just records and sends audio to a session for transcription.

---

## Text Injection

After transcription, the text needs to get into the IDE input.

### Method 1: tmux send-keys (terminal IDEs)

Same as v1. Requires the tmux alias (`claude-voice`).

```bash
tmux send-keys -t <tmux_session> -l "<text>"
tmux send-keys -t <tmux_session> Enter
```

**Works for:** Claude Code, Codex CLI

### Method 2: AppleScript keystroke (GUI IDEs)

For Cursor, VS Code, and other GUI editors that don't use tmux:

```applescript
tell application "System Events"
    tell process "<app_name>"
        set frontmost to true
        keystroke "<text>"
        keystroke return
    end tell
end tell
```

**Requires:** Accessibility permission for VoiceSmith.app
**Works for:** Cursor, VS Code, any GUI app with a text input

### Method 3: Clipboard + paste (fallback)

```applescript
set the clipboard to "<text>"
tell application "System Events"
    keystroke "v" using command down
    keystroke return
end tell
```

**Works everywhere** but overwrites the clipboard.

### Routing Decision

The menu bar app checks each session's injection method:

1. If `tmux_session` is set → use tmux send-keys
2. If the session's IDE is a GUI app (Cursor, VS Code) → use AppleScript
3. Fallback → clipboard + paste

The session registry could include an `injection_method` field, or the menu bar app infers it from the `tmux_session` presence.

---

## Multi-Session Routing

Same logic as v1, but now the menu bar app handles it directly:

1. Read `sessions.json` for active sessions (already polling every 1-5s)
2. If only one session → route there
3. If multiple sessions → parse first word of transcription:
   - Matches a session name (case-insensitive) → route to that session, strip name
   - No match → route to most recently active session (lowest `last_tool_call_age_s`)
4. Inject text via the appropriate method

**Edge cases:**

| Case | Behavior |
|------|----------|
| No active sessions | macOS notification: "No active sessions. Start a coding session first." |
| Session dies during recording | Notification: "Session disconnected." Fall back to next session or abort. |
| All sessions busy (listen active) | Queue the text and inject when any session becomes available, or inject immediately (it queues in the IDE) |
| User says only the session name | Don't inject empty text. Notification: "No message after session name." |
| Transcription is empty | Notification: "Didn't catch that." Resume listening. |
| Transcription confidence < 0.3 | Notification: "Didn't catch that clearly." Resume listening. |

---

## Mic Ownership & Handoff

### How the Socket Handles It

The `audio-service` accepts one client at a time. When a new client connects, the old client's connection is dropped (gets EOF). This means:

1. **Menu bar is listening for wake word** → connected to socket
2. **AI calls `listen`** → MCP server's `mic_capture.py` connects to socket → menu bar gets EOF → enters YIELDED state
3. **AI listen completes** → server disconnects → menu bar reconnects → enters LISTENING state

**No explicit handoff protocol needed.** The socket's single-client behavior handles it.

### What If the Menu Bar is Recording?

If the user says "Hey listen" and is speaking, then the AI also tries to listen at the same time:

1. Menu bar is in RECORDING state, connected to socket
2. AI's `listen` tool connects → menu bar gets EOF mid-recording
3. Menu bar saves whatever audio it captured so far
4. Menu bar enters YIELDED state, waits for AI to finish
5. After AI finishes → menu bar reconnects, but the interrupted recording is lost

**Mitigation:** This is unlikely (the user initiated wake word, so the AI shouldn't be calling listen at the same moment). If it happens, the menu bar shows a notification: "Recording interrupted by AI listen."

### What If the Audio-Service Isn't Running?

If the LaunchAgent isn't installed or the audio-service crashed:

1. Menu bar tries to connect to `/tmp/voicesmith-audio.sock`
2. Connection fails → wake word feature shows as "unavailable" in menu
3. Retry every 30 seconds
4. Once audio-service starts → connection succeeds → wake listening begins

---

## Feature Toggle

### Menu Bar Toggle

The existing quick toggles section in the menu panel gets a "Wake Word" toggle (currently hidden, waiting for implementation):

- **On:** Spawns the wake detector subprocess, connects to audio socket, starts listening
- **Off:** Kills the detector subprocess, disconnects from socket, releases mic

Toggle writes `wake_word.enabled` to config.json (via direct write, not HTTP — the menu bar app handles wake, not the server).

### Config

```json
{
  "wake_word": {
    "enabled": false,
    "model": "hey_listen",
    "threshold": 0.5,
    "ready_sound": "tink",
    "recording_timeout": 10,
    "no_speech_timeout": 5
  }
}
```

---

## Installation

### What Changes in the Installer

The `--with-voice-wake` flag now also:
1. Pip-installs `openwakeword` into the venv (same as v1)
2. Creates `wake_detector.py` in the install dir
3. Downloads the wake word model to `models/hey_listen.onnx`
4. Still creates tmux configs (for terminal IDE injection)
5. Enables `wake_word.enabled: true` in config

The VoiceSmith.app menu bar binary is always installed (it's useful without wake word too). The wake word feature just adds the Python detector and model.

### Dependencies

| Dependency | Size | Required by | New in v2? |
|-----------|------|-------------|-----------|
| openWakeWord | ~2MB pip | Wake detection | No (same as v1) |
| tmux | ~1MB brew | Text injection (terminal IDEs) | No (same as v1) |
| hey_listen.onnx | ~200KB | Wake word model | No (same as v1) |
| VoiceSmith.app | ~1MB | Menu bar + wake host | Yes (already built) |

No new dependencies beyond what v1 required. The menu bar app is already installed.

---

## New Server Endpoint: POST /transcribe

Accepts pre-recorded audio for transcription without opening the mic.

**Request:**
```
POST /transcribe
Content-Type: application/octet-stream

<raw float32 audio data, 16kHz mono>
```

**Response:**
```json
{
  "success": true,
  "text": "add error handling to the login",
  "confidence": 0.92,
  "duration_ms": 2100,
  "transcription_ms": 200
}
```

This lets the menu bar app record audio via the socket and send it to any session for transcription, reusing the loaded Whisper model with zero additional memory.

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `wake_detector.py` | Create | Standalone Python script: connects to audio socket, runs openWakeWord, outputs events to stdout |
| `menubar/VoiceSmithMenu.swift` | Modify | Add wake state machine, subprocess management, recording, injection, toggle |
| `server.py` | Modify | Add `POST /transcribe` endpoint |
| `stt/mic_capture.py` | — | No changes (socket-based recording already works) |
| `config.py` | — | No changes (wake_word config already exists) |
| `config.json` | — | No changes (wake_word section already exists) |
| `bin/install.js` | Modify | Create wake_detector.py during install |
| `install.sh` | Modify | Same |

---

## Security

- Wake word detection runs locally, no network
- Audio is processed in-memory, never saved to disk
- tmux `send-keys -l` prevents shell injection (same as v1)
- AppleScript keystroke requires Accessibility permission — user must grant
- The wake detector subprocess runs under the same user
- No new network endpoints — `/transcribe` is localhost-only (same as all HTTP endpoints)

---

## Verification

1. Enable wake word in menu bar toggle → icon shows wake is active
2. Say "Hey listen" → Tink sound, icon goes orange
3. Say "add error handling" → text appears in Claude Code and submits
4. Say "Hey listen" + say nothing → 5s timeout, resumes listening
5. Multiple sessions → say "Hey listen, Nova, run tests" → routes to Nova
6. AI calls `speak_then_listen` → wake yields mic (icon changes), then resumes after
7. Kill a session mid-recording → notification, resumes listening
8. Audio-service not running → wake shows "unavailable", retries
9. Disable wake in menu → detector subprocess killed, mic released
10. Enable wake in menu → detector spawns, listening resumes
11. Close all sessions → wake still listening (shows notification if triggered with no sessions)
12. Uninstall → wake detector and model removed

---

## Limitations

- **macOS only** — audio-service is CoreAudio, AppleScript is macOS
- **Requires audio-service LaunchAgent** — without it, wake word can't access mic
- **Python subprocess for detection** — adds ~20MB memory. Future: native ONNX Runtime in Swift
- **"Hey listen" only for v1** — custom wake phrases need model training
- **tmux still required for terminal IDEs** — the alias and wrapper are unchanged from v1
- **Single wake phrase for all sessions** — can't have per-session wake words in v1

---

## Future Enhancements

- **Native ONNX Runtime in Swift** — eliminate Python subprocess for wake detection
- **Custom wake word training** — `npx voicesmith-mcp train-wake-word "Hey Eric"`
- **Per-session wake phrases** — different wake word per session name
- **GUI IDE injection via MCP** — use MCP protocol to inject text instead of AppleScript
- **Streaming transcription** — start transcribing while user is still speaking
- **Wake word confidence display** — show detection confidence in menu bar
- **"Hey listen, all"** — broadcast command to all sessions
