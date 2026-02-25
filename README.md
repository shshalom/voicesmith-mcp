<p align="center">
  <img src="header.png" alt="VoiceSmith MCP" width="600">
</p>

# VoiceSmith MCP

Local AI voice for coding assistants. Gives your AI a real voice (text-to-speech) and ears (speech-to-text) via the Model Context Protocol (MCP). Fully offline — no cloud APIs, no data leaves your machine.

## What You Get

- **54 distinct voices** via Kokoro ONNX (local TTS, ~300MB model)
- **Speech-to-text** via faster-whisper (local STT, ~150MB model)
- **Voice activity detection** via Silero VAD (local, 2MB)
- **Multi-session support** — run multiple Claude Code sessions, each with its own voice (single session for Cursor/Codex)
- **Works with Claude Code, Cursor, and Codex**

## Quick Start

```bash
npx voicesmith-mcp install
```

The installer will:
1. Check system dependencies (Python 3.11+, espeak-ng, mpv)
2. Set up a Python virtual environment with all packages
3. Download TTS and STT models
4. Configure your IDE's MCP settings
5. Let you pick a voice
6. Inject voice behavior rules so the AI knows how to speak

Restart your IDE session after installing. The AI will greet you by voice on the first response.

## Usage

**Everything works out of the box.** After installing, just start a session — the AI speaks automatically. No configuration needed.

The installer sets up voice behavior rules that teach the AI when and how to use its voice:
- Opens with a brief spoken acknowledgment when you give it a task
- Closes with a spoken summary when it's done
- Asks questions out loud and listens for your voice response
- Falls back to text if voice tools aren't available

### Changing Voices Mid-Session

Ask the AI to switch voices at any time:

> "Switch to Nova"

If the voice is available, the AI will switch immediately. If it's occupied by another session, the AI will tell you and show available alternatives.

You can also browse all 54 voices:

> "Show me the available voices"

Or run `npx voicesmith-mcp voices` in a terminal to preview them.

### Voice Persistence

When you switch voices, the choice is saved. Next time you start or resume a session, the AI will use the same voice — no need to switch again.

### Muting

In a meeting or shared space? Ask the AI to mute:

> "Mute the voice"

The AI continues working normally — it just won't play audio. Say "unmute" when you're ready.

## Alternative Install

If you don't have Node.js or prefer a shell script:

```bash
git clone https://github.com/shshalom/voicesmith-mcp.git
cd voicesmith-mcp
./install.sh
```

Supports the same flags: `--claude`, `--cursor`, `--codex`, `--all`.

## MCP Tools

Once installed, your AI assistant has access to these tools:

| Tool | Description |
|------|-------------|
| `speak` | Synthesize and play speech for a named agent |
| `listen` | Open the mic, record speech, return transcribed text |
| `speak_then_listen` | Speak a question, then immediately listen for the answer |
| `set_voice` | Change the voice for an agent name |
| `get_voice_registry` | See which voices are assigned and available |
| `list_voices` | Browse all 54 Kokoro voices |
| `mute` / `unmute` | Silence or resume voice output |
| `stop` | Stop playback or cancel an active recording |
| `status` | Server health and session info |

## How It Works

The MCP server runs as a local process alongside your IDE. It communicates over stdio (the MCP protocol). All processing happens on your machine:

- **TTS**: Kokoro ONNX — fast neural TTS, 54 voices, no GPU needed
- **STT**: faster-whisper — OpenAI Whisper running locally via CTranslate2
- **VAD**: Silero VAD — voice activity detection for clean recordings
- **Audio**: mpv for playback, sounddevice for recording

## Multi-Session

**Claude Code:** Full multi-session support. Multiple Claude Code sessions can run simultaneously, each with its own voice. Session identity is tracked via Claude's `session_id` — resuming a session reclaims the same voice, and multiple terminals sharing the same session share the same voice. Orphaned servers are detected and cleaned up automatically.

**Cursor / Codex:** Single session only. Cursor runs one MCP server per config (shared across tabs), and Codex has no multi-session hooks. Voice works normally — just no multi-session coordination.

Cross-session audio is serialized via `flock` to prevent overlapping playback.

## Configuration

Config lives at `~/.local/share/voicesmith-mcp/config.json`. Key settings:

```json
{
  "main_agent": "Eric",
  "tts": {
    "default_voice": "am_eric",
    "audio_player": "mpv"
  },
  "stt": {
    "model_size": "base",
    "language": "en",
    "vad_threshold": 0.3
  }
}
```

Re-run `npx voicesmith-mcp install` to change your voice or update settings. Existing configuration is preserved — only new defaults are added.

## Requirements

- **Python 3.11+** (3.11 or 3.12 recommended)
- **macOS** (primary platform) or Linux (partial support)
- **espeak-ng** — phoneme backend for Kokoro
- **mpv** — audio playback
- ~500MB disk space for models

## Supported IDEs

| IDE | Config Location | Rules Location | Multi-Session |
|-----|----------------|----------------|---------------|
| Claude Code | `~/.claude.json` | `~/.claude/CLAUDE.md` | Yes (via session_id) |
| Cursor | `~/.cursor/mcp.json` | `~/.cursor/rules/voicesmith.mdc` | No (single server) |
| Codex | `~/.codex/mcp.json` | `~/.codex/AGENTS.md` | No (single session) |

## Uninstall

```bash
npx voicesmith-mcp uninstall
```

Removes all files, models, MCP config entries, and voice rules cleanly.

## License

Apache 2.0
