# Agent Voice MCP Server

A Model Context Protocol (MCP) server that provides local, high-quality text-to-speech **and** speech-to-text capabilities to AI coding assistants. Built on Kokoro ONNX (TTS) and faster-whisper (STT) for fast, fully offline voice interaction — enabling AI agents to speak with distinct voices and listen to user responses during development sessions.

Works with Claude Code, Cursor, VS Code, Windsurf, Zed, JetBrains, and any other tool that supports the MCP standard.

---

## Background

AI coding assistants are text-only by default. This project adds a full voice layer so that:
- The main AI agent speaks responses aloud (bookend pattern: opening + closing voice)
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
│              Agent Voice MCP Server                   │
│                                                       │
│  ┌────────────────────┐   ┌────────────────────────┐ │
│  │   TTS Engine        │   │   STT Engine            │ │
│  │                     │   │                         │ │
│  │  Kokoro ONNX (82M)  │   │  faster-whisper (150M)  │ │
│  │  53 voices           │   │  Silero VAD (2MB)       │ │
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

The server uses **stdio** transport (JSON-RPC over stdin/stdout). This is the standard for Claude Code and most MCP clients. Each IDE session spawns its own server process. Two concurrent sessions = two separate processes (~450MB each), with independent registries and queues. They do not collide.

### Logging

Server logs to **stderr** (MCP convention — stdout is reserved for the protocol). Optionally logs to `~/.local/share/agent-voice-mcp/server.log` when `"log_file": true` is set in config. Log levels: `debug`, `info`, `warn`, `error` (configurable via `"log_level"` in config, default: `info`).

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
   - **Pool exhaustion:** When all 53 voices are assigned, new agents get a hash-based voice from the full pool (may share with an existing agent). A warning is logged: "All voices assigned, reusing voices."

4. **Configurable main agent voice** — The main agent's voice is not hardcoded. Users set it via:
   - `config.json` (`"main_agent"` field)
   - The `set_voice` tool at runtime
   - Or simply by calling `speak` with any name — the first agent to speak becomes the "main" unless configured otherwise

5. **Voice state: tool presence = voice on** — When the MCP server is running, voice tools are available and the AI uses them. When the server is not running, the tools don't exist and the AI falls back to text. No on/off flag to manage. Additionally, `mute`/`unmute` tools allow temporarily silencing audio without stopping the server.

6. **Voice Activity Detection (VAD)** — The `listen` tool uses Silero VAD (2MB neural network) to detect when the user stops speaking. After 1.5 seconds of silence, recording stops and transcription begins. No manual "stop" action needed.

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
  "total": 53
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

Assign or reassign a voice to an agent name.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | Yes | Agent name to assign. |
| `voice` | string | Yes | Kokoro voice ID (e.g., "am_eric"). Must be a valid voice from `list_voices`. |

**Returns:**
```json
{
  "success": true,
  "name": "Eric",
  "voice": "am_eric"
}
```

**Invalid voice ID:** If the voice ID doesn't exist in the 53 available voices, returns `{ "success": false, "error": "invalid_voice", "message": "Voice 'am_xyz' not found. Use list_voices to see available options." }`

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

**Returns:**
```json
{
  "speak": { "success": true, "voice": "am_eric", "duration_ms": 1200 },
  "listen": { "success": true, "text": "Go with REST", "confidence": 0.96 }
}
```

### Diagnostic Tools

#### `status`

Report server health and component status. Always available, even if TTS or STT failed to load.

**Parameters:** None

**Returns:**
```json
{
  "tts": { "loaded": true, "model": "kokoro-v1.0.onnx", "voices": 53 },
  "stt": { "loaded": true, "model": "whisper-base", "language": "en" },
  "vad": { "loaded": true },
  "muted": false,
  "uptime_s": 3600,
  "registry_size": 3,
  "queue_depth": 0
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
1. Mic activates (user sees 🎙️ indicator)
2. Silero VAD monitors for speech
3. User speaks naturally
4. VAD detects 1.5s of silence → recording stops
5. faster-whisper transcribes the audio (~0.2s)
6. Text returned to AI as tool result
7. If timeout reached with no speech → returns `{ "success": false, "error": "timeout" }`
8. If cancelled via MCP cancellation notification or `stop` tool → returns `{ "success": false, "cancelled": true }`
9. If muted → returns immediately: `{ "success": false, "error": "muted", "message": "Voice input is muted" }`

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

The `confidence` field in `listen` responses is computed as `exp(avg_log_prob)` where `avg_log_prob` is the average log probability across all segments returned by faster-whisper. For single-segment transcriptions (typical for short commands), this is the direct segment probability. Range: 0.0 to 1.0.

### Voice Activity Detection: Silero VAD

| Property | Value |
|----------|-------|
| Size | 2MB |
| Type | Neural network |
| Latency | ~30ms |
| What it detects | Speech vs silence, ignores keyboard typing, fan noise, coughs |
| Silence threshold | 1.5s (configurable) |

### How `listen` Works Internally

```
AI calls listen(timeout=15)
    │
    ▼
┌─────────────────────────────────┐
│  1. Activate microphone          │
│     Show 🎙️ indicator           │
├─────────────────────────────────┤
│  2. Silero VAD monitors audio    │
│     Waiting for speech...        │
├─────────────────────────────────┤
│  3. Speech detected              │
│     Recording audio buffer       │
├─────────────────────────────────┤
│  4. 1.5s silence detected        │
│     Stop recording               │
├─────────────────────────────────┤
│  5. faster-whisper transcribes   │
│     (~0.2s for short commands)   │
├─────────────────────────────────┤
│  6. Return text to AI            │
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

### MCP Config (`.mcp.json` or Claude Desktop config)

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "~/.local/share/agent-voice-mcp/.venv/bin/python3",
      "args": ["~/.local/share/agent-voice-mcp/server.py"]
    }
  }
}
```

> **Note:** The installer writes the actual venv Python path during setup. The server finds its config and models from the standard locations. Environment variable overrides are available (see below) but not required for normal use.

### Server Configuration File (`config.json`)

**Runtime location:** `~/.local/share/agent-voice-mcp/config.json` (created during install)
**Lookup order:** `$AGENT_VOICE_CONFIG` env var → `~/.local/share/agent-voice-mcp/config.json` → built-in defaults
The `config.json` in the project repo root is the **default template** copied during install.

```json
{
  "tts": {
    "model_path": "~/.local/share/agent-voice-mcp/models/kokoro-v1.0.onnx",
    "voices_path": "~/.local/share/agent-voice-mcp/models/voices-v1.0.bin",
    "default_voice": "am_eric",
    "default_speed": 1.0,
    "audio_player": "mpv"
  },
  "stt": {
    "model_size": "base",
    "language": "en",
    "silence_threshold": 1.5,
    "max_listen_timeout": 15
  },
  "main_agent": "Eric",
  "voice_registry": {},
  "log_level": "info",
  "log_file": false
}
```

The `voice_registry` is **optional** — it starts empty by default. Voices are auto-assigned as agents speak. Users can pre-populate it to persist specific assignments across sessions, or to pin favorite voices to agent names.

The `main_agent` field identifies which agent name is the primary/lead agent. This is informational — it does not restrict other agents from speaking.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENT_VOICE_CONFIG` | Override path to config.json | `~/.local/share/agent-voice-mcp/config.json` |
| `KOKORO_MODEL` | Path to kokoro-v1.0.onnx model file | `~/.local/share/agent-voice-mcp/models/kokoro-v1.0.onnx` |
| `KOKORO_VOICES` | Path to voices-v1.0.bin file | `~/.local/share/agent-voice-mcp/models/voices-v1.0.bin` |
| `WHISPER_MODEL` | faster-whisper model size | `base` |
| `VOICE_PLAYER` | Audio player command | `mpv` |
| `VOICE_DEFAULT` | Default voice ID | `am_eric` |

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

2. **Hash-based assignment** — If no name match is found, a deterministic hash of the agent name selects from the unassigned voice pool. This ensures the same agent name always gets the same voice, even across restarts (if not pre-configured).

3. **Override anytime** — Users or agents can call `set_voice` to change any assignment.

### Main Agent Voice

The main agent's voice is **not hardcoded**. Users choose it via:
- `config.json` → `"default_voice": "am_eric"` (persists across sessions)
- `set_voice` tool at runtime (session-only unless saved to config)

### All Available Voices (53 total)

**American English (20):**
- Female: af_alloy, af_aoede, af_bella, af_heart, af_jessica, af_kore, af_nicole, af_nova, af_river, af_sarah, af_sky
- Male: am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael, am_onyx, am_puck, am_santa

**British English (8):**
- Female: bf_alice, bf_emma, bf_isabella, bf_lily
- Male: bm_daniel, bm_fable, bm_george, bm_lewis

**Other Languages (25):**
- Spanish: ef_dora, em_alex, em_santa
- French: ff_siwis
- Hindi: hf_alpha, hf_beta, hm_omega, hm_psi
- Italian: if_sara, im_nicola
- Japanese: jf_alpha, jf_gongitsune, jf_nezumi, jf_tebukuro, jm_kumo
- Portuguese: pf_dora, pm_alex, pm_santa
- Mandarin: zf_xiaobei, zf_xiaoni, zf_xiaoxiao, zf_xiaoyi, zm_yunjian, zm_yunxi, zm_yunxia, zm_yunyang

---

## Voice Behavior Rules (CLAUDE.md Template)

The MCP server handles the **mechanics** (synthesize, play, record, transcribe). The **behavioral rules** — when the AI should speak, the bookend pattern, sub-agent handoffs — are defined in a separate CLAUDE.md template file.

### Installation

The installer creates a voice rules file at `~/.claude/agents/voice-rules.md` which the user references from their `~/.claude/CLAUDE.md`:

```markdown
<!-- In ~/.claude/CLAUDE.md -->
See also: ~/.claude/agents/voice-rules.md
```

### Template Contents (voice-rules.md)

The template defines:

1. **Bookend speaking pattern** — The AI speaks twice per response:
   - Opening voice: brief acknowledgment, runs in background parallel with work (never blocking)
   - Closing voice: mandatory summary, especially when asking questions

2. **When to speak** — Acknowledge user requests, summarize outcomes, ask questions, report errors

3. **Sub-agent voice rules** — Each sub-agent speaks when starting work, finishing, blocked, or handing off to another agent

4. **Handoff protocol** — When Agent A hands work to Agent B, both speak: A announces the handoff, B acknowledges

5. **Voice state awareness** — "If the `speak` tool is available, use voice. If not, respond in text only."

6. **Listen behavior** — When asking the user a question, call `listen()` to get a voice response. Fall back to text input gracefully.

This template is **not injected into CLAUDE.md automatically** — it lives as a separate file that the user opts into by referencing it. This avoids conflicts with existing rules.

> **Note:** The full template text is a separate implementation deliverable, not included in this spec. The structure above defines the required sections. The actual wording will be written during build, informed by the proven voice rules from the development sessions that led to this project.

---

## Dependencies

### Runtime
- **Python 3.11+** (3.11 or 3.12 recommended; 3.13/3.14 have compatibility issues with some deps)
- **kokoro-onnx** — Kokoro TTS with ONNX Runtime (no PyTorch needed)
- **faster-whisper** — Whisper STT with CTranslate2 optimization
- **silero-vad** — Voice Activity Detection (speech vs silence)
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
- ~450MB RAM for loaded models (300MB TTS + 150MB STT)
- ~500MB disk for model files
- Microphone access (for STT)

---

## Installation

### Recommended: npx (one command, no cloning)

```bash
npx agent-voice-mcp install
```

This is the simplest way to install. Run it from anywhere — no need to clone a repo or navigate to a folder. The installer handles everything interactively.

### What the installer does

```
🎙️  Agent Voice MCP — Local AI Voice System

Step 1/6: Checking system dependencies...
  ✓ Python 3.12 found
  ✓ espeak-ng found (or installing via brew)
  ✓ mpv found (or installing via brew)

Step 2/6: Creating Python environment...
  ✓ Created venv at ~/.local/share/agent-voice-mcp/
  ✓ Installed kokoro-onnx, faster-whisper, silero-vad, mcp, sounddevice

Step 3/6: Downloading models (490MB)...
  ✓ kokoro-v1.0.onnx (310MB)
  ✓ voices-v1.0.bin (27MB)
  ✓ whisper-base model (~150MB)

Step 4/6: Configuring MCP server...
  ✓ Added to ~/.claude/mcp.json

Step 5/6: Requesting microphone access...
  🎙️ macOS will ask for microphone permission — please click "Allow"
  ✓ Microphone access granted

Step 6/6: Setting up voice rules...
  ? Choose your main agent voice:
    ❯ am_eric (male, confident)
      af_nova (female, clear)
      am_onyx (male, deep)
      am_adam (male, neutral)
      [browse all 53 voices]

  ✓ Voice rules saved to ~/.claude/agents/voice-rules.md
  ✓ Reference added to ~/.claude/CLAUDE.md

🎉 Done! Start a new Claude Code session to hear your AI speak.
   Run "npx agent-voice-mcp test" to hear a sample voice.
```

### Other npx commands

```bash
npx agent-voice-mcp install      # Full interactive setup
npx agent-voice-mcp test         # Play a sample voice to verify
npx agent-voice-mcp voices       # Browse and preview all 53 voices
npx agent-voice-mcp config       # Re-run voice picker / change settings
npx agent-voice-mcp uninstall    # Remove everything cleanly
```

### Uninstall

```bash
npx agent-voice-mcp uninstall
```

Prompts for confirmation, then removes:
- Python venv at `~/.local/share/agent-voice-mcp/`
- Models (490MB) at `~/.local/share/agent-voice-mcp/models/`
- Config at `~/.local/share/agent-voice-mcp/config.json`
- Server log at `~/.local/share/agent-voice-mcp/server.log`
- MCP server registration from `~/.claude/mcp.json`
- Voice rules at `~/.claude/agents/voice-rules.md`

Does **not** remove: npm cache (managed by npm), references in user's CLAUDE.md (manual cleanup — we don't edit their CLAUDE.md without explicit consent).

### Alternative: Git clone (for contributors)

```bash
git clone https://github.com/<user>/agent-voice-mcp.git
cd agent-voice-mcp
./install.sh
```

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
agent-voice-mcp/
├── SPEC.md                # This file
├── README.md              # User-facing documentation
├── LICENSE                # Apache 2.0
├── package.json           # npm package (for npx agent-voice-mcp)
├── bin/
│   └── cli.js             # npx entry point (install, test, voices, config, uninstall)
├── install.sh             # Alternative setup script (for git clone)
├── config.json            # Default server configuration
├── server.py              # MCP server entry point
├── tts/
│   ├── kokoro_engine.py   # Kokoro ONNX wrapper (model loading, synthesis)
│   ├── speech_queue.py    # Sequential speech queue (prevents overlap)
│   └── audio_player.py    # Audio playback (mpv, afplay, aplay)
├── stt/
│   ├── whisper_engine.py  # faster-whisper wrapper (model loading, transcription)
│   ├── mic_capture.py     # Microphone recording with sounddevice
│   └── vad.py             # Silero VAD integration (silence detection)
├── voice_registry.py      # Auto-discovery voice registry (name matching + hash)
├── requirements.txt       # Python dependencies
├── templates/
│   └── voice-rules.md     # CLAUDE.md voice behavior template
├── models/                # Model files (downloaded during install)
│   ├── kokoro-v1.0.onnx
│   └── voices-v1.0.bin
└── tests/
    ├── test_tts.py
    ├── test_stt.py
    ├── test_registry.py
    └── test_server.py
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
| `test_stt.py` | Whisper engine loading, transcription accuracy, VAD silence detection timing, confidence score computation, timeout behavior |
| `test_registry.py` | Name matching (exact + case-insensitive), hash-based assignment, pool exhaustion + wrap-around, persistence (save/load config.json), set_voice override |
| `test_server.py` | MCP tool call routing, speak block/non-block modes, mute/unmute state transitions, concurrent listen rejection, stop cancelling listen, speak_then_listen atomic flow, status tool output, graceful degradation when engines fail |

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

| Feature | Agent Voice MCP | Edge TTS | macOS `say` | OpenAI TTS MCP |
|---------|----------------|----------|-------------|----------------|
| Runs locally | Yes | No (cloud) | Yes | No (cloud) |
| TTS latency | <1s | 15-20s | Instant | 1-3s |
| STT included | Yes | No | No | No |
| Voice quality | Very good | Excellent | Medium | Excellent |
| Voices | 53 | 322 | ~20 | 6 |
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

All three trigger: stop any active playback/recording → save registry → exit 0.

### Python Discovery (npx installer)

The `cli.js` npm entry point locates a compatible Python in this order:

1. `python3.12` → `python3.11` → `python3` → `python`
2. Validates version with `--version` (requires 3.11+)
3. Checks for existing venv at `~/.local/share/agent-voice-mcp/`
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
