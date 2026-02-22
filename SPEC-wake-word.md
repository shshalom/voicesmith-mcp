# Wake Word Detection — Design Spec

## Context

The Agent Voice MCP Server provides AI-initiated listening via the `listen` and `speak_then_listen` tools. This spec adds **user-initiated voice input** — the user says a wake phrase and their speech is transcribed and delivered to the correct IDE session, without touching the keyboard or focusing the window.

## Overview

- **Wake word engine:** openWakeWord (ONNX Runtime, local, ~200KB model)
- **Text injection:** tmux send-keys (zero permissions, no window focus needed)
- **Wake phrase:** "Hey listen" (single pre-trained model shipped with the package)
- **Optional feature:** Installed with `--withVoiceWake` flag, toggleable at runtime
- **Transparent UX:** Shell alias wraps `claude` in tmux invisibly — all flags and shortcuts work as normal

---

## Architecture

```
User says: "Hey listen... add error handling to the login"
        │
        ▼
┌──────────────────────────────────────────────┐
│  MCP Server (background thread)               │
│                                               │
│  openWakeWord listener                        │
│  ├── Monitors mic continuously (80ms frames)  │
│  ├── Detects "Hey listen" (confidence > 0.5)  │
│  ├── Pauses wake listener                     │
│  ├── Plays Tink ready sound                   │
│  ├── Records speech (VAD + sounddevice)       │
│  ├── Whisper transcribes                      │
│  ├── tmux send-keys → target session          │
│  └── Resumes wake listener                    │
└──────────────────────────────────────────────┘
        │
        ▼  tmux send-keys -t agent-voice-eric "add error handling to the login" Enter
        │
        ▼  Text appears in Claude Code prompt and submits
```

### How the User Launches Claude

The installer creates a shell initialization script and adds a single source line to the user's shell profile.

**File: `~/.local/share/agent-voice-mcp/shell-init.sh`**

Contains all functions, aliases, and tmux configuration:

```bash
#!/bin/bash
# Agent Voice MCP — Shell initialization
# Sourced from ~/.zshrc or ~/.bashrc

# Only set up the alias if wake word feature is enabled
if [ -f "$HOME/.local/share/agent-voice-mcp/config.json" ]; then
    _agent_voice_wake_enabled=$(python3 -c "
import json
with open('$HOME/.local/share/agent-voice-mcp/config.json') as f:
    print(json.load(f).get('wake_word', {}).get('enabled', False))
" 2>/dev/null)

    if [ "$_agent_voice_wake_enabled" = "True" ]; then
        claude-voice() {
            if [ -n "$TMUX" ]; then
                # Already in tmux — just set the env var and run claude directly
                export AGENT_VOICE_TMUX=$(tmux display-message -p '#S')
                command claude "$@"
            else
                local session_name="agent-voice-$$"
                tmux -f "$HOME/.local/share/agent-voice-mcp/tmux.conf" \
                    new-session -s "$session_name" \
                    -e "AGENT_VOICE_TMUX=$session_name" \
                    "command claude $*"
            fi
        }
        alias claude='claude-voice'
    fi
    unset _agent_voice_wake_enabled
fi
```

**File: `~/.local/share/agent-voice-mcp/tmux.conf`**

Minimal tmux config for invisible operation:

```
# Agent Voice MCP — tmux config (invisible mode)
set -g status off
set -g mouse on
set -g prefix None
set -g escape-time 0
```

**Added to `~/.zshrc` (or `~/.bashrc`):**

```bash
# agent-voice-mcp
[ -f ~/.local/share/agent-voice-mcp/shell-init.sh ] && source ~/.local/share/agent-voice-mcp/shell-init.sh
```

**Benefits:**
- `.zshrc` stays clean — one line
- All logic lives in the package — easy to update without touching shell config
- Uninstall removes the source line + the files
- If the package is removed, the source line silently does nothing (file check)
- The alias is only created if wake word is enabled in config
- Existing tmux users are handled (detects `$TMUX`)

**User experience:** The user types `claude` exactly as before. All flags work (`-c`, `-p`, `-r`, `--model`, etc.). All keyboard shortcuts pass through. tmux is invisible — no status bar, no visible change.

---

## Wake Word Listener

### Lifecycle

The wake word listener runs as a **daemon thread** inside the MCP server process. It starts when the server starts (if the feature is enabled) and stops on shutdown.

**States:**

```
┌──────────┐    server starts     ┌───────────┐
│ DISABLED │ ──────────────────── │  IDLE      │
│          │  (if feature off)    │ (not       │
└──────────┘                      │  listening)│
                                  └─────┬─────┘
                                        │ feature enabled
                                        ▼
                                  ┌───────────┐
                              ┌── │ LISTENING  │ ◄─────────────────┐
                              │   │ (wake word)│                   │
                              │   └─────┬─────┘                   │
                              │         │ wake detected            │
                              │         ▼                          │
                              │   ┌───────────┐                   │
                              │   │ RECORDING  │                   │
                              │   │ (speech)   │                   │
                              │   └─────┬─────┘                   │
                              │         │ silence / transcribed    │
                              │         ▼                          │
                              │   ┌───────────┐                   │
                              │   │ INJECTING  │ ──────────────────┘
                              │   │ (tmux)     │    resume listening
                              │   └───────────┘
                              │
                              │  AI calls listen/speak_then_listen
                              │         │
                              │         ▼
                              │   ┌───────────┐
                              └── │ YIELDED    │ ── AI listen done ─┐
                                  │ (mic given │                    │
                                  │  to AI)    │                    │
                                  └───────────┘                    │
                                        ▲                          │
                                        └──────────────────────────┘
```

### Mic Ownership

Only one component can use the mic at a time:

| State | Mic owner | What's happening |
|-------|-----------|-----------------|
| LISTENING | Wake word listener | Processing 80ms int16 frames through openWakeWord |
| RECORDING | Wake word listener | Recording speech with VAD (float32, 512-sample chunks) |
| YIELDED | AI (listen tool) | AI called listen/speak_then_listen, wake listener paused |
| DISABLED | Nobody | Feature turned off |

**Handoff protocol:**
1. AI calls `listen` → server sets a flag → wake listener sees flag, pauses mic, signals ready
2. AI's `listen` tool opens mic, records, transcribes, returns
3. AI's `listen` tool signals done → wake listener resumes

### Audio Format Switching

openWakeWord needs int16 at 16kHz, 1280-sample blocks.
VAD/Whisper needs float32 at 16kHz, 512-sample blocks.

When switching from wake word detection to speech recording:
1. Close the int16 stream
2. Flush audio buffers (prevents wake phrase echo in transcription)
3. Open a float32 stream with 512-sample blocksize
4. Record until VAD detects silence

---

## Wake Word Model

### Shipped Model: "Hey Listen"

A custom openWakeWord model trained for the phrase "Hey listen", shipped as a ~200KB ONNX file in the npm package.

**Training:** Done once via openWakeWord's Google Colab notebook:
1. Generate synthetic speech clips of "Hey listen" using TTS (multiple voices, speeds, accents)
2. Generate negative examples (random speech, "Hey Siri", "Hey Google", etc.)
3. Train the classification head on top of openWakeWord's frozen feature extractor
4. Export as ONNX (~200KB)
5. Bundle in `models/hey_listen.onnx`

**Until the custom model is trained:** Ship with "hey_jarvis_v0.1" as a working stand-in. Users can say "Hey Jarvis" to trigger. The custom model is a drop-in replacement.

**Detection threshold:** 0.5 (configurable via `config.json`)

### Future: Custom Wake Words

A future `npx agent-voice-mcp train-wake-word "Hey Nova"` command could run the training pipeline locally or via Colab. Not in scope for v1.

---

## Multi-Session Routing

### Session Name from tmux

Each Claude Code session runs in a tmux session named `agent-voice-<PID>`. The MCP server reads `$AGENT_VOICE_TMUX` to know its own tmux session name.

### Routing Logic

When the wake word triggers and speech is transcribed:

1. Read `sessions.json` to get all active sessions
2. If only one session → send text there (no name parsing needed)
3. If multiple sessions → parse the first word of the transcription:
   - Matches a session name (case-insensitive) → route to that session, strip the name from the message
   - No match → route to the most recently started session, keep full text
4. `tmux send-keys -t <session_name> "<message>" Enter`

**Example:**
```
Transcription: "Eric add error handling"
Sessions: [Eric:agent-voice-123, Nova:agent-voice-456]
→ Matches "Eric" → send "add error handling" to agent-voice-123

Transcription: "run the tests"
Sessions: [Eric:agent-voice-123]
→ Only one session → send "run the tests" to agent-voice-123
```

---

## Feature Toggle

### Install-time

The wake word feature is optional. Installed with:
```bash
npx agent-voice-mcp install --withVoiceWake
```

Without the flag, the feature is not installed:
- openWakeWord is not pip-installed
- tmux is not required
- Shell alias is not created
- Wake word models are not downloaded

### Runtime Toggle

Two new MCP tools:

**`wake_enable`** — Start the wake word listener
```json
{ "success": true, "wake_word": "hey_listen", "listening": true }
```

**`wake_disable`** — Stop the wake word listener, release mic
```json
{ "success": true, "listening": false }
```

Also configurable in `config.json`:
```json
{
  "wake_word": {
    "enabled": true,
    "model": "hey_listen",
    "threshold": 0.5
  }
}
```

The `status` tool includes wake word state:
```json
{
  "wake_word": {
    "enabled": true,
    "listening": true,
    "model": "hey_listen",
    "tmux_session": "agent-voice-12345"
  }
}
```

---

## Installation Details

### What `--withVoiceWake` adds to the install

**Step 1 (system deps):** Also checks for tmux, installs via brew if missing.

**Step 2 (Python env):** Also pip-installs `openwakeword`.

**Step 3 (models):** Also downloads the "Hey listen" wake word model to `models/hey_listen.onnx`.

**Step 6 (voice rules):** Also:
- Creates `~/.local/share/agent-voice-mcp/shell-init.sh` (functions + alias)
- Creates `~/.local/share/agent-voice-mcp/tmux.conf` (invisible tmux config)
- Adds one source line to `~/.zshrc` (or `~/.bashrc`):
```bash
# agent-voice-mcp
[ -f ~/.local/share/agent-voice-mcp/shell-init.sh ] && source ~/.local/share/agent-voice-mcp/shell-init.sh
```

**Sentinel comment** for clean uninstall — the source line is identified by the `# agent-voice-mcp` comment.

### Uninstall

`npx agent-voice-mcp uninstall` also:
- Removes the source line from `~/.zshrc` / `~/.bashrc`
- Removes `shell-init.sh` and `tmux.conf` (part of the install dir cleanup)
- Removes the wake word model
- Removes openWakeWord pip package (if no other package depends on it)

---

## Dependencies (Wake Word Feature Only)

| Dependency | Size | Purpose |
|-----------|------|---------|
| openWakeWord | ~2MB (pip) | Wake word detection |
| tmux | ~1MB (brew) | Text injection without focus |
| hey_listen.onnx | ~200KB | Wake word model |

Total additional footprint: ~3MB. No torch, no GPU.

---

## Configuration

### `config.json` additions

```json
{
  "wake_word": {
    "enabled": false,
    "model": "hey_listen",
    "threshold": 0.5,
    "ready_sound": "/System/Library/Sounds/Tink.aiff"
  }
}
```

`enabled` defaults to `false`. Set to `true` by the installer when `--withVoiceWake` is used.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_VOICE_TMUX` | tmux session name (set by shell alias) | none |
| `VOICE_WAKE_ENABLED` | Override wake word on/off | from config |

---

## Files to Create/Modify

| File | Action | What |
|------|--------|------|
| `wake_listener.py` | Create | Wake word listener thread (openWakeWord + recording + tmux inject) |
| `shell-init.sh` | Create | Shell functions + alias (sourced from .zshrc) |
| `tmux.conf` | Create | Minimal invisible tmux configuration |
| `server.py` | Modify | Start/stop wake listener, new tools (wake_enable/disable), mic handoff |
| `config.py` | Modify | Add wake_word config section |
| `config.json` | Modify | Add wake_word defaults |
| `shared.py` | Modify | Add wake word constants |
| `bin/install.js` | Modify | `--withVoiceWake` flag: install tmux, openwakeword, shell-init, model |
| `bin/uninstall.js` | Modify | Remove source line from shell profile, clean up |
| `install.sh` | Modify | Same additions for shell installer |
| `models/hey_listen.onnx` | Create | Trained wake word model (or hey_jarvis stand-in) |
| `tests/test_wake.py` | Create | Wake listener tests |

---

## Security

- Wake word listener runs locally, no network
- Mic audio is processed in-memory, never saved to disk
- tmux send-keys is local IPC, no network
- The shell alias only affects the user who installed it
- Feature is opt-in (`--withVoiceWake`)

---

## Verification

1. `npx agent-voice-mcp install --withVoiceWake` → installs tmux, openwakeword, alias
2. Open new terminal → `claude` launches inside tmux (invisible)
3. `status` tool shows `wake_word.listening: true`
4. Say "Hey listen" → Tink sound plays
5. Say "hello world" → text appears in Claude Code prompt and submits
6. AI responds to "hello world"
7. Multi-session: start second session → say "Hey listen, Nova, run the tests" → routes to Nova
8. `wake_disable` tool → wake listener stops, mic released
9. `wake_enable` tool → wake listener resumes
10. `npx agent-voice-mcp uninstall` → alias removed from .zshrc, clean state

---

## Limitations

- **macOS and Linux only** — tmux not available on Windows
- **Terminal-based IDEs only** — Claude Code, Codex CLI. GUI editors (Cursor, VS Code) need a different injection method (future work)
- **One mic at a time** — Wake listener yields mic when AI listens, may miss wake words during AI listen calls
- **"Hey listen" only for v1** — Custom wake phrases require model training (future)
- **tmux required** — Adds a dependency, though the alias makes it transparent

---

## Future Enhancements

- Custom wake word training: `npx agent-voice-mcp train-wake-word "Hey Eric"`
- GUI editor support via InputMethodKit or sendkeys
- Visual indicator (menu bar icon) when wake listener is active
- Configurable post-wake-word timeout
- Wake word sensitivity tuning per environment (noisy vs quiet)
- "Hey listen, all" to broadcast to all sessions
