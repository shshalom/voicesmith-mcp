# VoiceSmith MCP Server

A Model Context Protocol (MCP) server that provides local, high-quality text-to-speech **and** speech-to-text capabilities to AI coding assistants. Built on Kokoro ONNX (TTS) and faster-whisper (STT) for fast, fully offline voice interaction — enabling AI agents to speak with distinct voices and listen to user responses during development sessions.

Works with Claude Code, Cursor, VS Code, Windsurf, Zed, JetBrains, and any other tool that supports the MCP standard.

---

## Background

AI coding assistants are text-only by default. This project adds a full voice layer so that:
- The main AI agent speaks responses aloud (closing voice always, opening voice when meaningful)
- Sub-agents and team agents each have distinct voices, creating a "team of people" feel
- Handoffs between agents are audible — you hear who's talking without reading
- The AI can listen to your spoken responses — no typing needed when asked a question
- All synthesis and transcription runs **locally** with zero network dependency and sub-second latency

### Why not cloud TTS/STT?

We started with Microsoft Edge TTS (cloud-based) and experienced 15-20 second latency per voice call due to network round-trips. Switching to Kokoro ONNX (local TTS) reduced this to under 1 second on Apple Silicon. For STT, faster-whisper runs the same Whisper model locally with no API key or subscription. The MCP server keeps both models loaded in memory, eliminating per-call overhead.

---

## Architecture

### Overview

```
AI Assistant (Claude Code, Cursor, etc.)
    │
    │  MCP tool calls
    ▼
┌──────────────────────────────────────────────────────┐
│              VoiceSmith MCP Server                   │
│                                                       │
│  ┌────────────────────┐   ┌────────────────────────┐ │
│  │   TTS Engine        │   │   STT Engine            │ │
│  │                     │   │                         │ │
│  │  Kokoro ONNX (82M)  │   │  faster-whisper (150M)  │ │
│  │  54 voices           │   │  Silero VAD (2MB)       │ │
│  │  loaded at startup   │   │  loaded at startup      │ │
│  └────────────────────┘   └────────────────────────┘ │
│                                                       │
│  ┌────────────────────┐   ┌────────────────────────┐ │
│  │  Voice Registry     │   │  Audio I/O              │ │
│  │  (auto-discovery)   │   │  Mic capture + Playback │ │
│  └────────────────────┘   └────────────────────────┘ │
│                                                       │
│  ┌────────────────────┐                               │
│  │  Speech Queue       │   Memory: ~450MB total       │
│  │  (prevents overlap) │   (300MB TTS + 150MB STT)    │
│  └────────────────────┘                               │
└──────────────────────────────────────────────────────┘
```

### Transport

The server uses **stdio** transport (JSON-RPC over stdin/stdout). On macOS, the IDE spawns `VoiceSmithMCP.app` (a native C launcher) which forks the Python server and forwards signals — this ensures the process runs inside a signed app bundle for TCC microphone permission. On Linux, the Python server is spawned directly. Each IDE session spawns its own server process. Two concurrent sessions = two separate processes (~450MB each), with independent registries and queues. They do not collide.

### Logging

Server logs to **stderr** (MCP convention — stdout is reserved for the protocol). Optionally logs to `~/.local/share/voicesmith-mcp/server.log` when `"log_file": true` is set in config. Log levels: `debug`, `info`, `warn`, `error` (configurable via `"log_level"` in config, default: `info`).

### Platform Support

- **macOS** — Fully supported. Apple Silicon recommended for best performance.
- **Linux** — Supported. x86_64 and ARM64.
- **Windows** — Not supported in v1. Planned for v2.

### Key Design Decisions

1. **Persistent process** — Both Kokoro TTS and faster-whisper STT load once at startup (~2.2s total), then stay in memory (~450MB). All subsequent calls skip model loading entirely.

2. **Internal speech queue** — TTS requests are serialized automatically. No file-based locking (talking stick), no race conditions, no stale locks. If two agents call `speak` simultaneously, the second waits for the first to finish.

3. **Auto-discovery voice registry** — When an agent calls `speak` with a name the server hasn't seen before, it automatically assigns an unused voice from the pool and registers it. No pre-configuration needed. The assignment algorithm:
   - First, check if the name matches a Kokoro voice name (e.g., agent "Eric" → `am_eric`, agent "Nova" → `af_nova`)
   - If no name match, pick a deterministic but unique voice from the unassigned pool (using a hash of the agent name)
   - The assigned voice persists for the server's lifetime and is **auto-saved to config.json** on graceful shutdown and periodically (every 60s), so assignments survive restarts
   - Users can optionally pre-configure mappings in `config.json` for guaranteed persistence
   - **Pool exhaustion:** When all 54 voices are assigned, new agents get a hash-based voice from the full pool (may share with an existing agent). A warning is logged: "All voices assigned, reusing voices."

4. **Configurable main agent voice** — The main agent's voice is not hardcoded. Users set it via:
   - `config.json` (`"main_agent"` field)
   - The `set_voice` tool at runtime
   - Or simply by calling `speak` with any name — the first agent to speak becomes the "main" unless configured otherwise

5. **Voice state: tool presence = voice on** — When the MCP server is running, voice tools are available and the AI uses them. When the server is not running, the tools don't exist and the AI falls back to text. No on/off flag to manage. Additionally, `mute`/`unmute` tools allow temporarily silencing audio without stopping the server.

6. **Voice Activity Detection (VAD)** — The `listen` tool uses Silero VAD (2MB ONNX model, runs on ONNX Runtime — no PyTorch dependency) to detect when the user stops speaking. After 1.5 seconds of silence, recording stops and transcription begins. No manual "stop" action needed. The VAD requires a 64-sample context window prepended to each 512-sample chunk for accurate detection. The speech detection threshold is configurable via `stt.vad_threshold` in config.json (default: 0.3 — lowered from 0.5 for better sensitivity to softer speech).

7. **Temp file auto-cleanup** — Audio files are generated to a temp path, played, and immediately deleted. No accumulation.

8. **Cross-platform audio** — Uses `mpv` for playback (macOS, Linux). Falls back to system commands (`afplay` on macOS, `aplay` on Linux) if mpv is unavailable.

---

## MCP Tools

### TTS Tools (Voice Output)

#### `speak`

Synthesize and play speech for a named agent.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | Yes | Agent name (e.g., "Eric", "Nova"). Maps to a voice via the registry. Unknown names are **auto-assigned** a unique voice from the available pool and registered for future calls. |
| `text` | string | Yes | The text to speak. |
| `speed` | number | No | Speech speed multiplier. Default: 1.0 |
| `block` | boolean | No | Whether to wait for playback to complete before returning. Default: true |

**Returns (block: true):**
```json
{
  "success": true,
  "voice": "am_eric",
  "auto_assigned": false,
  "duration_ms": 850,
  "synthesis_ms": 420
}
```

**Returns (block: false):** Fire-and-forget. Returns immediately after queuing (before synthesis or playback). No callback/poll mechanism — intended for the opening voice in the bookend pattern where the AI doesn't need to wait.
```json
{
  "success": true,
  "voice": "am_eric",
  "auto_assigned": false,
  "queued": true
}
```

`auto_assigned` is `true` when the server assigned a new voice for a previously unknown agent name.

**Returns (name occupied):** When the AI uses the preferred voice name (configured `main_agent` or `last_voice_name`) but this session was assigned a different name because another session already claimed it:
```json
{
  "success": false,
  "error": "name_occupied",
  "message": "'Eric' is occupied by another session. This session is 'Adam'. Use name='Adam' instead.",
  "session_name": "Adam",
  "session_voice": "am_adam"
}
```

The AI should inform the user and show available voices via `get_voice_registry` — never silently fall back to a different voice.

**Text handling:** Plain text only — no SSML or markup supported. For long text (>500 characters), the server **auto-chunks** by sentence (splits on `.` `!` `?`), synthesizes each chunk, and plays them sequentially with no gap. No hard rejection or length limit — just auto-chunking. This also enables future streaming playback (play chunk 1 while synthesizing chunk 2).

#### `list_voices`

List all available Kokoro voices.

**Parameters:** None

**Returns:**
```json
{
  "voices": [
    { "id": "am_eric", "gender": "male", "accent": "american" },
    { "id": "af_nova", "gender": "female", "accent": "american" },
    ...
  ],
  "total": 54
}
```

#### `get_voice_registry`

Get current agent-to-voice mappings. This registry is built dynamically — entries are added as agents speak for the first time, or via `set_voice`.

**Parameters:** None

**Returns:**
```json
{
  "registry": {
    "default": "am_eric",
    "Eric": "am_eric",
    "Fenrir": "am_fenrir"
  },
  "available_pool": ["af_nova", "am_onyx", "af_heart", "am_adam", "..."],
  "total_assigned": 2,
  "total_available": 51
}
```

#### `set_voice`

Assign or reassign a voice to an agent name. **Also renames the session** so the name and voice always match. The name is derived from the voice ID (e.g., `am_fenrir` → "Fenrir").

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | Yes | Agent name to assign. |
| `voice` | string | Yes | Kokoro voice ID (e.g., "am_eric"). Must be a valid voice from `list_voices`. |

**Returns (rename):**
```json
{
  "success": true,
  "name": "Fenrir",
  "voice": "am_fenrir",
  "previous_name": "Liam"
}
```

`previous_name` is only present when the session was renamed (name changed). If the name didn't change (same voice reassigned), it's omitted.

**Returns (name occupied):** If the derived name is already taken by another active session:
```json
{
  "success": false,
  "error": "name_occupied",
  "message": "'Fenrir' is occupied by another session."
}
```

**Invalid voice ID:** If the voice ID doesn't exist in the 54 available voices, returns `{ "success": false, "error": "invalid_voice", "message": "Voice 'am_xyz' not found. Use list_voices to see available options." }`

#### `stop`

Stop any currently playing audio **and** cancel any active `listen` recording. If a `listen` is in progress, it returns `{ "success": false, "cancelled": true }` to the caller.

**Parameters:** None

**Returns:**
```json
{
  "success": true,
  "stopped_playback": true,
  "cancelled_listen": false
}
```

#### `mute`

Temporarily silence all voice output. The `speak` tool still returns success but no audio plays. Useful when in a meeting or shared space.

**Parameters:** None

**Returns:**
```json
{
  "success": true,
  "muted": true
}
```

#### `unmute`

Resume voice output after muting.

**Parameters:** None

**Returns:**
```json
{
  "success": true,
  "muted": false
}
```

### Combined Tools

#### `speak_then_listen`

Convenience tool that speaks a question and immediately listens for the answer in one atomic call. Reduces two MCP round-trips to one — ideal for the "ask a question and wait for voice response" pattern.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | Yes | Agent name for the voice. |
| `text` | string | Yes | The question to speak. |
| `speed` | number | No | Speech speed. Default: 1.0 |
| `timeout` | number | No | Max seconds to wait for response. Default: 15 |
| `silence_threshold` | number | No | Seconds of silence before stopping. Default: 1.5 |

**Returns (success):**
```json
{
  "speak": { "success": true, "voice": "am_eric", "duration_ms": 1200 },
  "listen": { "success": true, "text": "Go with REST", "confidence": 0.96 }
}
```

**Returns (timeout — no speech detected):** When the user doesn't respond within the timeout, the tool automatically speaks a nudge ("I didn't catch that. Go ahead and type it.") and returns:
```json
{
  "speak": { "success": true, "voice": "am_eric", "duration_ms": 1200 },
  "listen": { "success": false, "error": "timeout", "message": "No speech detected within timeout", "nudge_spoken": true }
}
```

The AI should then wait for typed input — never retry `listen`.

### Diagnostic Tools

#### `status`

Report server health and component status. Always available, even if TTS or STT failed to load.

**Parameters:** None

**Returns:**
```json
{
  "tts": { "loaded": true, "model": "kokoro-v1.0.onnx", "voices": 54 },
  "stt": { "loaded": true, "model": "whisper-base", "language": "en" },
  "vad": { "loaded": true },
  "muted": false,
  "uptime_s": 3600,
  "registry_size": 3,
  "queue_depth": 0,
  "session": {
    "name": "Eric",
    "voice": "am_eric",
    "port": 7865,
    "pid": 12345
  }
}
```

### STT Tools (Voice Input)

#### `listen`

Activate the microphone, record the user's speech, and return the transcribed text. Uses Silero VAD to automatically detect when the user stops speaking (1.5s silence threshold).

This is a **blocking** tool call — the AI waits while the user speaks, then receives the transcribed text as the tool result. No keyboard input needed from the user.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `timeout` | number | No | Maximum seconds to wait for speech. Default: 15 |
| `prompt` | string | No | Optional context about what the AI is asking (for logging/display). |
| `silence_threshold` | number | No | Seconds of silence before stopping. Default: 1.5 |

**Returns:**
```json
{
  "success": true,
  "text": "Go with REST",
  "confidence": 0.96,
  "duration_ms": 2100,
  "transcription_ms": 200
}
```

**Behavior:**
1. **Tink ready sound plays** — a short audio cue (macOS system sound) so the user knows to start speaking. Skipped when called from push-to-talk HTTP endpoint (which has its own beep).
2. Mic activates
3. Silero VAD monitors for speech
4. User speaks naturally
5. VAD detects 1.5s of silence → recording stops
6. faster-whisper transcribes the audio (~0.2s)
7. Text returned to AI as tool result
8. If timeout reached with no speech → returns `{ "success": false, "error": "timeout" }`
9. If cancelled via MCP cancellation notification or `stop` tool → returns `{ "success": false, "cancelled": true }`
10. If muted → returns immediately: `{ "success": false, "error": "muted", "message": "Voice input is muted" }`

**Cancellation:** The server is a stdio subprocess with no TTY access — it cannot detect keystrokes directly. Cancellation is handled through two mechanisms:
1. **MCP protocol cancellation** — The client sends a `notifications/cancelled` message for the in-progress `listen` request. The server respects this and stops recording immediately.
2. **The `stop` tool** — Another tool call that interrupts any active `listen` (already specified above).

The "press Escape to cancel" experience is a **client-side UX concern** — the IDE detects Escape and sends one of the above signals. The server itself never sees keystrokes.

**Concurrent calls:** Only one `listen` can be active at a time (one microphone). If a second `listen` is called while one is in progress, it returns immediately: `{ "success": false, "error": "mic_busy", "message": "Another listen call is in progress" }`. The first call is not interrupted.

**Fallback:** If `listen` returns cancelled or timeout, the AI should not re-call `listen`. Instead, fall back to requesting text input.

---

## Voice State Management

### Tool Presence = Voice On

The primary on/off mechanism is simply whether the MCP server is running:

- **Server running** → `speak`, `listen`, and all voice tools are available → AI uses voice
- **Server not running** → tools don't exist in the AI's toolbox → AI uses text only

No configuration flags or toggles needed. The CLAUDE.md voice rules instruct the AI: "If the `speak` tool is available, use voice. If not, use text."

### Mute/Unmute (Temporary Silence)

When the server IS running but you want to temporarily silence it:

- `mute()` → `speak` calls return success silently (no audio). `listen` is also disabled.
- `unmute()` → normal voice resumes.

| Scenario | What happens |
|----------|-------------|
| Server off | No voice tools available → text only |
| Server on | Voice works normally |
| Server on + muted | `speak` returns success but no audio plays |
| Server on + unmuted | Back to normal voice |

---

## STT Architecture

### Engine: faster-whisper

| Property | Value |
|----------|-------|
| Model | OpenAI Whisper (open source, MIT license) |
| Runtime | CTranslate2 (optimized inference) |
| Speed | ~4x faster than real-time on Apple Silicon |
| Accuracy | Excellent (same as OpenAI Whisper API) |
| Model size | base: 150MB, small: 500MB, medium: 1.5GB |
| Languages | 99 |
| Network | None (fully local) |
| Cost | Free |

### Confidence Score Computation

The `confidence` field in `listen` responses is computed as `exp(avg_logprob)` where `avg_logprob` is the average log probability across all segments returned by faster-whisper (note: the attribute is `avg_logprob`, not `avg_log_prob`). For single-segment transcriptions (typical for short commands), this is the direct segment probability. Range: 0.0 to 1.0.

### Voice Activity Detection: Silero VAD (ONNX)

| Property | Value |
|----------|-------|
| Size | 2MB |
| Type | Neural network (ONNX) |
| Runtime | ONNX Runtime (shared with Kokoro TTS — no PyTorch dependency) |
| Latency | ~30ms |
| What it detects | Speech vs silence, ignores keyboard typing, fan noise, coughs |
| Silence threshold | 1.5s (configurable) |
| Chunk size | 512 samples at 16kHz (32ms) + 64-sample context window |

### Microphone Capture: Three Backends

On macOS, the Python process running through a terminal doesn't get a microphone permission dialog — macOS TCC (Transparency, Consent, and Control) silently denies it, resulting in all-zero audio. The fix: native C audio capture inside a signed app bundle (`VoiceSmithMCP.app`) with `NSMicrophoneUsageDescription`.

| Backend | Platform | How it works | When used |
|---------|----------|--------------|-----------|
| **Socket (primary)** | macOS | `audio-service` LaunchAgent streams CoreAudio via Unix socket (`/tmp/voicesmith-audio.sock`) | macOS with LaunchAgent installed |
| **Subprocess (fallback)** | macOS | `audio-capture` binary spawned by Python, streams via stdout | macOS without LaunchAgent |
| **sounddevice (fallback)** | Linux/other | PortAudio-based capture via sounddevice library | Non-macOS platforms |

All three backends produce identical output: **16kHz mono float32 in 512-sample chunks** (matching Silero VAD requirements). The VAD detection loop is shared across all backends.

#### App Bundle Architecture (macOS)

```
VoiceSmithMCP.app/
├── Contents/
│   ├── Info.plist              # NSMicrophoneUsageDescription for TCC
│   └── MacOS/
│       ├── VoiceSmithMCP       # MCP launcher (forks Python server)
│       ├── audio-service       # LaunchAgent daemon (CoreAudio → Unix socket)
│       └── audio-capture       # Subprocess recorder (CoreAudio → stdout)
```

The installer compiles these from C source, codesigns the bundle with `com.voicesmith-mcp.launcher`, and installs a LaunchAgent (`com.voicesmith-mcp.audio`) that runs `audio-service` under launchd. Because launchd is the parent (not the terminal), macOS TCC attributes mic access to the app bundle and shows a proper permission dialog.

#### TCC Denial Detection

If the mic returns all-zero samples for ~320ms, the server detects silent audio (TCC denial) and returns an error with a helpful message pointing the user to System Settings > Privacy & Security > Microphone.

### How `listen` Works Internally

```
AI calls listen(timeout=15)
    │
    ▼
┌─────────────────────────────────┐
│  1. Play Tink ready sound        │
├─────────────────────────────────┤
│  2. Reset VAD state              │
│     Open mic (socket/subprocess/ │
│     sounddevice)                 │
│     Flush first 200ms (speaker   │
│     bleed prevention)            │
├─────────────────────────────────┤
│  3. Silero VAD monitors audio    │
│     Waiting for speech...        │
├─────────────────────────────────┤
│  4. Speech detected              │
│     Recording audio buffer       │
├─────────────────────────────────┤
│  5. 1.5s silence detected        │
│     Stop recording               │
├─────────────────────────────────┤
│  6. faster-whisper transcribes   │
│     (~0.2s for short commands)   │
├─────────────────────────────────┤
│  7. Return text to AI            │
│     { "text": "Go with REST" }   │
└─────────────────────────────────┘
```

### User-Initiated Voice Input (Push-to-Talk) — v2 / Client-Side

> **Note:** This is a client-side feature, not part of the MCP server. It depends on IDE-specific hooks (Claude Code hooks, Cursor extensions, etc.) and is planned for v2.

For when the user wants to speak without the AI asking:

```
User types: /voice (or presses configured hotkey)
→ Client-side hook calls the MCP listen tool
→ User speaks
→ VAD detects silence → transcribe
→ Text auto-submitted to AI as input (via AppleScript/keystroke injection)
```

This bypasses the terminal's Enter key requirement — text is injected and submitted programmatically. Implementation details are IDE-specific and out of scope for the MCP server itself.

---

## Full Conversation Flow Example

```
User types: "Add webhook support"

🔊 Eric: "Got it, working on it."
   (AI works, spawns architect agent)

🔊 Fenrir: "We'll need a POST endpoint with HMAC validation."
   (AI spawns explorer)

🔊 Nova: "Found the existing route patterns."

🔊 Fenrir: "Onyx, scaffold the route."

🔊 Onyx: "On it... Done. Ready for review."

🔊 Eric: "Looks good. Should I add rate limiting too?"
   AI calls: listen(timeout=15, prompt="Add rate limiting?")
   🎙️ Mic ON
   User speaks: "Yeah, add rate limiting"
   🎙️ Mic OFF → transcribe → "Yeah, add rate limiting"

🔊 Eric: "Adding rate limiting now."
   (AI works...)

🔊 Eric: "All done. Webhook with rate limiting is ready."
```

No typing needed for the user's response. No Enter key. Fully hands-free when the AI asks questions.

---

## Configuration

### MCP Config (per IDE)

The installer writes the MCP server entry to the correct config file for each selected IDE:

| IDE | Config Path |
|-----|------------|
| **Claude Code** | `~/.claude.json` |
| **Cursor** | `~/.cursor/mcp.json` |
| **Codex (OpenAI)** | `~/.codex/mcp.json` |

All use the same server entry format:
```json
{
  "mcpServers": {
    "voicesmith": {
      "command": "~/.local/share/voicesmith-mcp/.venv/bin/python3",
      "args": ["~/.local/share/voicesmith-mcp/server.py"]
    }
  }
}
```

> **Note:** The installer writes the actual venv Python path during setup. Use `--claude`, `--cursor`, `--codex`, or `--all` flags to target specific IDEs, or let the installer auto-detect installed IDEs.

### Server Configuration File (`config.json`)

**Runtime location:** `~/.local/share/voicesmith-mcp/config.json` (created during install)
**Lookup order:** `$VOICESMITH_CONFIG` env var → `~/.local/share/voicesmith-mcp/config.json` → built-in defaults
The `config.json` in the project repo root is the **default template** copied during install.

```json
{
  "tts": {
    "model_path": "~/.local/share/voicesmith-mcp/models/kokoro-v1.0.onnx",
    "voices_path": "~/.local/share/voicesmith-mcp/models/voices-v1.0.bin",
    "default_voice": "am_eric",
    "default_speed": 1.0,
    "audio_player": "mpv"
  },
  "stt": {
    "model_size": "base",
    "language": "en",
    "silence_threshold": 1.5,
    "max_listen_timeout": 15,
    "vad_threshold": 0.3
  },
  "main_agent": "Eric",
  "last_voice_name": null,
  "voice_registry": {},
  "log_level": "info",
  "log_file": false,
  "http_port": 7865
}
```

The `last_voice_name` tracks the user's last voice switch via `set_voice`. When set, the server uses it as the preferred name on next startup instead of `main_agent`. This enables voice persistence across session restarts/resumes. Set to `null` to always use `main_agent`.

The `http_port` configures the base port for the HTTP listener (used for push-to-talk and session health checks). Each concurrent session claims the next available port starting from this base.

The `voice_registry` is **optional** — it starts empty by default. Voices are auto-assigned as agents speak. Users can pre-populate it to persist specific assignments across sessions, or to pin favorite voices to agent names.

The `main_agent` field identifies which agent name is the primary/lead agent. This is informational — it does not restrict other agents from speaking.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `VOICESMITH_CONFIG` | Override path to config.json | `~/.local/share/voicesmith-mcp/config.json` |
| `KOKORO_MODEL` | Path to kokoro-v1.0.onnx model file | `~/.local/share/voicesmith-mcp/models/kokoro-v1.0.onnx` |
| `KOKORO_VOICES` | Path to voices-v1.0.bin file | `~/.local/share/voicesmith-mcp/models/voices-v1.0.bin` |
| `WHISPER_MODEL` | faster-whisper model size | `base` |
| `VOICE_PLAYER` | Audio player command | `mpv` |
| `VOICE_DEFAULT` | Default voice ID | `am_eric` |
| `VOICE_HTTP_PORT` | HTTP listener base port | `7865` |

---

## Voice Registry

### Auto-Discovery & Assignment

The voice registry is **dynamic**. No pre-configuration is required.

**When a new agent name calls `speak` for the first time:**

1. **Name matching** — If the agent name matches a Kokoro voice name (case-insensitive), that voice is assigned automatically. Examples:
   - Agent "Eric" → `am_eric`
   - Agent "Nova" → `af_nova`
   - Agent "Bella" → `af_bella`
   - Agent "George" → `bm_george`

2. **Priority-ordered assignment** — If no name match is found, the server picks from a priority-ordered list: American English voices first (male, then female), then British English, then all other languages. This ensures fallback voices are always English-speaking. Within the voice registry (for sub-agents), a hash-based assignment from the unassigned pool is used for deterministic selection.

3. **Override anytime** — Users or agents can call `set_voice` to change any assignment.

### Main VoiceSmith

The main agent's voice is **not hardcoded**. Users choose it via:
- `config.json` → `"default_voice": "am_eric"` (persists across sessions)
- `set_voice` tool at runtime (session-only unless saved to config)

### All Available Voices (54 total)

**American English (20):**
- Female (11): af_alloy, af_aoede, af_bella, af_heart, af_jessica, af_kore, af_nicole, af_nova, af_river, af_sarah, af_sky
- Male (9): am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael, am_onyx, am_puck, am_santa

**British English (8):**
- Female (4): bf_alice, bf_emma, bf_isabella, bf_lily
- Male (4): bm_daniel, bm_fable, bm_george, bm_lewis

**Other Languages (26):**
- Spanish: ef_dora, em_alex, em_santa
- French: ff_siwis
- Hindi: hf_alpha, hf_beta, hm_omega, hm_psi
- Italian: if_sara, im_nicola
- Japanese: jf_alpha, jf_gongitsune, jf_nezumi, jf_tebukuro, jm_kumo
- Portuguese: pf_dora, pm_alex, pm_santa
- Mandarin: zf_xiaobei, zf_xiaoni, zf_xiaoxiao, zf_xiaoyi, zm_yunjian, zm_yunxi, zm_yunxia, zm_yunyang

---

## Voice Behavior Rules (IDE-Specific Injection)

The MCP server handles the **mechanics** (synthesize, play, record, transcribe). The **behavioral rules** — when the AI should speak, the bookend pattern, sub-agent voice assignment — are injected directly into each IDE's instruction file during install.

### How It Works

The installer asks the user to pick a main agent voice (e.g., "Eric" → `am_eric`), then writes **personalized** behavior rules into the IDE's config:

| IDE | Rules Location | Method |
|-----|---------------|--------|
| **Claude Code** | `~/.claude/CLAUDE.md` | Appended block with sentinel comment |
| **Cursor** | `~/.cursor/rules/voicesmith.mdc` | Standalone MDC file with `alwaysApply: true` |
| **Codex (OpenAI)** | `~/.codex/AGENTS.md` | Appended block with sentinel comment |

All injected blocks are marked with `<!-- installed by voicesmith-mcp -->` for idempotent updates and clean uninstall.

### Rules Content

The rules are personalized with the chosen main agent name and teach the AI:

1. **Voice identity** — "You are **Eric**. Always call `speak` with `name: "Eric"`." Includes tone guidance: be conversational, match the user's energy.
2. **Voice switching** — If `speak` returns `name_occupied`, tell the user the voice is taken, call `get_voice_registry`, and show available voices. Never silently fall back.
3. **Speaking pattern** — Opening voice is optional (only when meaningful, e.g., clarifying approach — no filler). Closing voice (`block: true`) is mandatory, never skip.
4. **Voice for questions** — Use `speak_then_listen` only when the AI literally cannot continue without user input (choosing between options, confirming a destructive action). Rhetorical wrap-ups use regular `speak`.
5. **Speed preferences** — The `speak` tool accepts a `speed` parameter. If the user asks to speak slower/faster, adjust and remember for the session.
6. **Sub-agent voice assignment** — Pick names matching Kokoro voices, never reuse the main agent's name. On handoffs, both agents speak.
7. **Error handling** — If `speak` or `speak_then_listen` fails, fall back to text silently. No retries.
8. **Fallback** — Text-only when tools unavailable, respect muted state

### Template

The raw template is at `templates/voice-rules.md` with `{{MAIN_AGENT}}` placeholders. The installer fills these in per the user's voice choice.

---

## Dependencies

### Runtime
- **Python 3.11+** (3.11 or 3.12 recommended; 3.13/3.14 have compatibility issues with some deps)
- **kokoro-onnx** — Kokoro TTS with ONNX Runtime (no PyTorch needed)
- **faster-whisper** — Whisper STT with CTranslate2 optimization
- **silero-vad** — Voice Activity Detection via ONNX Runtime (no PyTorch — uses the same ONNX Runtime as Kokoro TTS)
- **soundfile** — Audio file I/O
- **sounddevice** — Microphone capture
- **mcp** — MCP Python SDK
- **mpv** — Audio playback (or system alternative)
- **espeak-ng** — Phonemizer backend (required by Kokoro)

### Model Files (downloaded during install)
- `kokoro-v1.0.onnx` — 310MB (TTS model)
- `voices-v1.0.bin` — 27MB (TTS voice embeddings)
- `whisper-base` — ~150MB (STT model, auto-downloaded by faster-whisper)
- `silero_vad.onnx` — ~2MB (VAD model)

### System Requirements
- macOS (Apple Silicon recommended) or Linux
- **Xcode Command Line Tools** (macOS only — needed to compile the native audio launcher)
- ~450MB RAM for loaded models (300MB TTS + 150MB STT)
- ~500MB disk for model files
- Microphone access (for STT — on macOS, the installer builds `VoiceSmithMCP.app` which triggers a proper TCC permission dialog)

---

## Installation

### Recommended: npx (one command, no cloning)

```bash
npx voicesmith-mcp install              # Auto-detect installed IDEs
npx voicesmith-mcp install --claude     # Claude Code only
npx voicesmith-mcp install --cursor     # Cursor only
npx voicesmith-mcp install --codex      # Codex (OpenAI) only
npx voicesmith-mcp install --all        # All supported IDEs
```

Run from anywhere — no need to clone a repo or navigate to a folder. The installer detects existing tools and models, skips what's already installed, and only downloads/installs what's missing.

### What the installer does

```
🎙️  VoiceSmith MCP — Local AI Voice System

Step 1/6: Checking system dependencies...
  ✓ Python 3.12 found
  ✓ espeak-ng found
  ✓ mpv found

Step 2/6: Setting up Python environment...
  ✓ Server files copied to ~/.local/share/voicesmith-mcp
  ✓ Created venv at ~/.local/share/voicesmith-mcp/.venv
  ✓ All packages installed

Step 3/6: Checking models...
  ✓ kokoro-v1.0.onnx already installed (or downloaded / symlinked from existing install)
  ✓ voices-v1.0.bin already installed
  ℹ whisper-base model (~150MB) will download automatically on first use

Step 4/6: Configuring MCP server...
  ℹ Detected: Claude Code, Cursor
  ✓ Claude Code: already configured
  ✓ Cursor: added to ~/.cursor/mcp.json

Step 5/6: Checking microphone access...
  ✓ Microphone access granted

Step 6/6: Setting up voice rules...
  Choose your main agent voice:
    1) Eric (am_eric — male, American, confident)
    2) Nova (af_nova — female, American, clear)
    ...
  ✓ Main agent voice: Eric (am_eric)
  ✓ Claude Code: voice rules added to ~/.claude/CLAUDE.md
  ✓ Cursor: voice rules written to ~/.cursor/rules/voicesmith.mdc

🎉 Done! Configured for: Claude Code, Cursor
   Restart your IDE session, then voice tools will be available.
   Run "npx voicesmith-mcp test" to hear a sample voice.
```

**Smart detection:** Re-running the installer shows all green checkmarks — it's fully idempotent. It also detects existing Kokoro model files (e.g., from `~/.local/share/kokoro-tts/models/`) and symlinks them instead of re-downloading.

**Config merge on upgrade:** When `config.json` already exists, the installer merges new default keys without overwriting user values. Nested objects (e.g., `stt`, `tts`) are merged at the sub-key level. This means upgrading picks up new settings (like `vad_threshold`) while preserving your voice choice and other customizations.

**Voice rules update:** The installer uses sentinel-based blocks (`<!-- installed by voicesmith-mcp -->`) to replace voice rules on re-install. Updated rules (e.g., new voice switching behavior) are applied automatically.

### Other npx commands

```bash
npx voicesmith-mcp install      # Full interactive setup
npx voicesmith-mcp test         # Play a sample voice to verify
npx voicesmith-mcp voices       # Browse and preview all 54 voices
npx voicesmith-mcp config       # Re-run voice picker / change settings
npx voicesmith-mcp uninstall    # Remove everything cleanly
```

### Uninstall

```bash
npx voicesmith-mcp uninstall
```

Prompts for confirmation, then removes:
- Python venv at `~/.local/share/voicesmith-mcp/`
- Models at `~/.local/share/voicesmith-mcp/models/`
- Config at `~/.local/share/voicesmith-mcp/config.json`
- Server log at `~/.local/share/voicesmith-mcp/server.log`
- MCP server entries from all configured IDEs (Claude Code, Cursor, Codex)
- Voice rules blocks from all IDE instruction files
- Voice rules template at `~/.claude/agents/voice-rules.md`

Does **not** remove: npm cache (managed by npm).

### Alternative: Git clone (for contributors)

```bash
git clone https://github.com/shshalom/voicesmith-mcp.git
cd voicesmith-mcp
./install.sh
```

The shell installer (`install.sh`) has full feature parity with the npx installer:
- Interactive voice picker (8 preset voices + custom)
- IDE auto-detection and `--claude`, `--cursor`, `--codex`, `--all` flags
- Smart package install (only installs missing packages)
- Venv health validation on re-install
- Config merge on upgrade
- Sentinel-based voice rules injection for all IDEs
- Legacy `~/.claude/mcp.json` cleanup

### Manual Install (power users)

```bash
# 1. Install system deps
brew install espeak-ng mpv  # macOS
# apt install espeak-ng mpv  # Linux

# 2. Create venv and install Python deps
python3.12 -m venv .venv
source .venv/bin/activate
pip install kokoro-onnx faster-whisper silero-vad soundfile sounddevice mcp

# 3. Download TTS model files
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# 4. Test
python server.py --test
```

---

## Project Structure

```
voicesmith-mcp/
├── SPEC.md                # This file
├── README.md              # User-facing documentation
├── LICENSE                # Apache 2.0
├── package.json           # npm package (for npx voicesmith-mcp)
├── .npmignore             # Excludes tests, pycache, wake word files from npm
├── bin/
│   ├── cli.js             # npx entry point — command router
│   ├── install.js         # 6-step interactive installer
│   ├── uninstall.js       # Clean removal with confirmation
│   ├── test-voice.js      # Quick smoke test
│   ├── voices.js          # Browse and preview all voices
│   ├── config.js          # Re-run voice picker / change settings
│   └── utils.js           # Shared helpers (Python discovery, IDE configs, logging)
├── hooks/
│   └── session-start.sh   # SessionStart hook — discovers assigned voice name
├── launcher/
│   ├── main.c             # Native MCP launcher (forks Python, forwards signals)
│   ├── audio_service.c    # LaunchAgent audio daemon (CoreAudio → Unix socket)
│   ├── mic_capture.c      # Subprocess audio recorder (CoreAudio → stdout fallback)
│   ├── Info.plist         # App bundle metadata with NSMicrophoneUsageDescription
│   └── com.voicesmith-mcp.audio.plist  # LaunchAgent config for audio service
├── install.sh             # Alternative setup script (full parity with npx installer)
├── config.json            # Default server configuration
├── server.py              # MCP server entry point (11 tools via FastMCP)
├── shared.py              # Constants, voice catalog, types, exceptions
├── config.py              # Configuration management (load/save with env overrides)
├── voice_registry.py      # Auto-discovery voice registry (name matching + hash)
├── session_registry.py    # Multi-session coordination (sessions.json, stale detection)
├── requirements.txt       # Python dependencies
├── tts/
│   ├── __init__.py
│   ├── kokoro_engine.py   # Kokoro ONNX wrapper (model loading, synthesis)
│   ├── speech_queue.py    # Sequential speech queue (prevents overlap)
│   └── audio_player.py    # Audio playback (mpv, afplay, aplay) with cross-session flock
├── stt/
│   ├── __init__.py
│   ├── whisper_engine.py  # faster-whisper wrapper (model loading, transcription)
│   ├── mic_capture.py     # Microphone recording (socket/subprocess/sounddevice backends)
│   └── vad.py             # Silero VAD via ONNX Runtime (configurable threshold)
├── templates/
│   └── voice-rules.md     # Voice behavior template ({{MAIN_AGENT}} placeholder)
├── models/                # Model files (downloaded during install)
│   ├── kokoro-v1.0.onnx
│   └── voices-v1.0.bin
└── tests/
    ├── conftest.py        # Shared mock fixtures
    ├── test_tts.py        # TTS tests
    ├── test_stt.py        # STT tests
    ├── test_registry.py   # Registry tests
    ├── test_server.py     # Integration tests
    └── test_wake.py       # Wake word listener tests (132 total)
```

---

## Testing

### Framework

**pytest** with optional model mocking for CI environments.

### Running Tests

```bash
# Activate venv
source .venv/bin/activate

# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run a specific test file
pytest tests/test_tts.py
```

### Test Coverage

| Test file | What it covers |
|-----------|---------------|
| `test_tts.py` | Kokoro engine loading, synthesis output format, voice selection, speed control, auto-chunking of long text, temp file cleanup |
| `test_stt.py` | Whisper engine loading, transcription accuracy, VAD silence detection timing (configurable threshold), confidence score computation, timeout behavior |
| `test_registry.py` | Name matching (exact + case-insensitive), priority-based assignment, pool exhaustion + wrap-around, persistence (save/load config.json), set_voice override |
| `test_server.py` | MCP tool call routing, speak block/non-block modes, name_occupied error, mute/unmute state transitions, concurrent listen rejection, stop cancelling listen, speak_then_listen atomic flow with timeout nudge, status tool output, graceful degradation when engines fail |
| `test_wake.py` | Wake word detection, mic handoff, tmux injection, recording timeout |

**Total: 132 tests** (all passing)

### CI

GitHub Actions with a macOS runner for Apple Silicon testing. Tests can run **without models** by mocking the TTS/STT engines — this keeps CI fast and avoids downloading 490MB of models on every run.

---

## Performance

Benchmarked on Apple M4 Max (64GB RAM):

### TTS Performance

| Metric | Shell Script (old) | MCP Server |
|--------|-------------------|------------|
| Model load | Every call (~1.3s) | Once at startup (0s after) |
| Synthesis (short) | ~0.8s | ~0.8s |
| Synthesis (medium) | ~0.9s | ~0.9s |
| Synthesis (long) | ~1.8s | ~1.8s |
| Total per call | ~2.1s | **~0.85s** |
| Memory usage | Transient | ~300MB persistent |
| Network required | No | No |
| Concurrent handling | Talking stick (file lock) | Internal queue |

Real-time factor: 0.12x (generates audio 8x faster than real-time)

### STT Performance (estimated)

| Metric | Value |
|--------|-------|
| Model load | Once at startup (~0.8s) |
| Transcription (5-word command) | ~0.2s |
| Transcription (15-word sentence) | ~0.4s |
| VAD latency | ~30ms |
| Silence detection | 1.5s (configurable) |
| Total listen-to-text | ~2.0s (1.5s silence + 0.2s transcribe + overhead) |
| Memory | ~150MB persistent |

### Server Startup

| Component | Load Time | Memory |
|-----------|-----------|--------|
| Kokoro TTS | ~1.3s | ~300MB |
| faster-whisper | ~0.8s | ~150MB |
| Silero VAD | ~0.1s | ~2MB |
| **Total** | **~2.2s** | **~450MB** |

---

## Comparison with Alternatives

| Feature | VoiceSmith MCP | Edge TTS | macOS `say` | OpenAI TTS MCP |
|---------|----------------|----------|-------------|----------------|
| Runs locally | Yes | No (cloud) | Yes | No (cloud) |
| TTS latency | <1s | 15-20s | Instant | 1-3s |
| STT included | Yes | No | No | No |
| Voice quality | Very good | Excellent | Medium | Excellent |
| Voices | 54 | 322 | ~20 | 6 |
| Multi-language | 8 languages | 74 languages | English-focused | ~50 languages |
| Cost | Free | Free (unofficial) | Free | Paid (API) |
| Voice cloning | No | No | No | No |
| Emotional speech | No | No | No | No |
| MCP native | Yes | No | No | Yes |
| Network dependency | None | Always | None | Always |
| Voice input | Yes (STT) | No | No | No |

---

## Startup & Error Handling

### Graceful Degradation

If a component fails to load at startup, the server degrades gracefully instead of refusing to start:

| Failure | Behavior |
|---------|----------|
| TTS fails (bad model path, etc.) | Server starts with STT tools only. `speak` returns error. `status` reports TTS as failed with diagnostics. |
| STT fails (missing model, etc.) | Server starts with TTS tools only. `listen` returns error. `status` reports STT as failed. |
| Both fail | Server refuses to start. Exits with clear error message to stderr including paths checked and versions found. |
| VAD fails | STT starts without VAD. `listen` uses a simple energy-based silence detector as fallback. |

The `status` tool is always available and reports the health of each component, making it easy to diagnose issues.

### Graceful Shutdown

The server saves the voice registry to config.json and exits cleanly on:
- **stdin EOF** — MCP client disconnects (normal session end)
- **SIGTERM** — Process terminated by the system or IDE
- **SIGINT** — Ctrl+C during manual testing

All three trigger: stop any active playback/recording → save registry → unregister session → exit 0.

### Stale Session Detection

A periodic background thread (every 60 seconds) cleans up stale sessions from `sessions.json`:

1. **PID check** — If the process is dead, remove the entry immediately.
2. **HTTP health check** — Ping the session's `/status` endpoint. If it doesn't respond, the server has crashed or is unreachable.
3. **Activity check** — The `/status` endpoint reports `last_tool_call_age_s` (seconds since the last MCP tool call). If this exceeds 5 minutes, the session is considered orphaned (process alive but no MCP client connected).

Stale sessions are killed with `SIGTERM` and removed from the registry. This prevents zombie processes from blocking voice names and ports indefinitely.

### Python Discovery (npx installer)

The `cli.js` npm entry point locates a compatible Python in this order:

1. `python3.12` → `python3.11` → `python3` → `python`
2. Validates version with `--version` (requires 3.11+)
3. Checks for existing venv at `~/.local/share/voicesmith-mcp/`
4. If no compatible Python found → shows clear error: "Python 3.11+ required. Install with: brew install python@3.12"

---

## Future Enhancements

- **Streaming TTS playback** — Start playing audio before synthesis completes (for long text)
- **Continuous conversation mode** — Wake word detection ("Hey Eric") for always-on voice
- **Voice cloning** — Support custom voice training with reference audio samples
- **Emotional tags** — If Orpheus TTS adds Apple Silicon support, integrate for `<laugh>`, `<sigh>` etc.
- **Conversation logging** — Record all agent speech to a conversation transcript
- **Web UI** — Dashboard to manage voices, test synthesis, view conversation history
- **Larger Whisper models** — Optional medium/large models for higher accuracy STT
