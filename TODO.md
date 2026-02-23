# Agent Voice MCP — TODO / Feature Tracker

## Status Legend
- 🔴 Not started
- 🟡 In progress
- 🟢 Done
- ⚪ Deferred / Future

---

## Core Features

### Wake Word Flow
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 1 | 🟢 | Wake word detection (openWakeWord) | "Hey listen" model trained and working |
| 2 | 🟢 | Cross-session mic lock | Only one session owns the wake mic (flock) |
| 3 | 🔴 | tmux alias integration | shell-init.sh written but not yet tested end-to-end with Claude Code |
| 4 | 🔴 | Full flow test: wake → record → transcribe → tmux inject | Blocked on tmux alias being active |
| 5 | 🟢 | Wake enable/disable MCP tools | `wake_enable`, `wake_disable` implemented |
| 6 | 🔴 | Multi-session name routing | Code written but untested with real tmux sessions |

### Session Management
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 7 | 🟢 | Session registry (sessions.json) | With flock, stale PID cleanup, tmux_session field |
| 8 | 🟢 | Auto-assign different voice for new sessions | If name taken, picks next available Kokoro voice |
| 9 | 🔴 | Resume with previous voice | When `claude -r` resumes, reclaim the same voice/name. Needs: detect resume, look up persisted registry, register with that name |
| 10 | 🟢 | Cross-session audio lock (flock) | Prevents overlapping TTS playback |

### UX Improvements
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 11 | 🔴 | Menu bar indicator for wake listener | Show mic state (active/recording/disabled), click to toggle. Use `rumps` (Python) or native Swift. |
| 12 | 🔴 | Session preheat / agent intro | On session start, AI speaks a short greeting ("Eric here, ready to go."). Via Claude Code `SessionStart` hook. Warms up TTS engine. |
| 13 | 🟢 | Tink ready sound after wake word | Plays before recording starts |
| 14 | 🔴 | Timeout nudge on listen | After listen timeout, AI speaks "I didn't catch that, go ahead and type it." |

### Installer
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 15 | 🟢 | Multi-IDE support (Claude Code, Cursor, Codex) | --claude, --cursor, --codex, --all flags |
| 16 | 🟢 | --with-voice-wake flag | Installs tmux, openwakeword, shell scripts, enables in config |
| 17 | 🟢 | Personalized voice rules injection | Per-IDE, with voice picker, sentinel comments |
| 18 | 🔴 | Deploy shell-init.sh and source line to .zshrc | Code written but not tested in installer flow |

### Voice Rules / Behavior
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 19 | 🟢 | Questions trigger speak_then_listen | Mandatory for input questions, not rhetorical |
| 20 | 🟢 | Sub-agents check get_voice_registry before naming | Documented in voice rules |
| 21 | 🟢 | Bookend pattern (opening + closing voice) | Opening non-blocking, closing mandatory |

---

## Known Issues

| # | Status | Issue | Notes |
|---|--------|-------|-------|
| 22 | 🟢 | Multiple sessions competing for mic | Fixed with wake mic flock |
| 23 | 🔴 | Stale sessions not cleaned up on crash | PID check works but only runs on next startup. Could add periodic cleanup. |
| 24 | 🔴 | tmux_session always null without alias | Wake word text injection doesn't work until user launches via tmux alias |
| 25 | 🔴 | Wake listener doesn't release mic cleanly when MCP server crashes | flock handles it, but sounddevice stream may leak. OS should clean up on process death. |

---

## Future / Deferred

| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 26 | ⚪ | Custom wake word training CLI | `npx agent-voice-mcp train-wake-word "Hey Nova"` — uses Colab or local training |
| 27 | ⚪ | GUI editor support (Cursor, VS Code) | Needs InputMethodKit, sendkeys, or editor extension for text injection |
| 28 | ⚪ | README.md | User-facing documentation |
| 29 | ⚪ | LICENSE file | Apache 2.0 |
| 30 | ⚪ | Publish to npm | package.json ready, needs npm account |
| 31 | ⚪ | GitHub Actions CI | macOS runner, mocked tests (no models) |
| 32 | ⚪ | Streaming TTS playback | Play chunk 1 while synthesizing chunk 2 |
| 33 | ⚪ | Linux ready sound fallback | Bundled WAV + aplay/paplay |
| 34 | ⚪ | Conversation logging | Record all agent speech to transcript |
| 35 | ⚪ | Larger Whisper models | Optional medium/large for higher accuracy |
| 36 | ⚪ | Visual wake indicator | Menu bar / notification when wake listener activates |
| 37 | ⚪ | "Hey listen, all" broadcast | Send to all sessions simultaneously |
| 38 | ⚪ | Windows support | tmux alternative, different audio stack |
| 39 | ⚪ | Wake word sensitivity tuning | Per-environment threshold (noisy vs quiet) |
| 40 | ⚪ | Periodic stale session cleanup | Background thread that cleans sessions.json every N seconds |

---

## Architecture Decisions (Recorded)

| Decision | Choice | Why |
|----------|--------|-----|
| TTS engine | Kokoro ONNX | Local, fast, 54 voices, ONNX Runtime |
| STT engine | faster-whisper | Local, accurate, same as OpenAI Whisper API |
| VAD | Silero VAD (ONNX) | Local, 2MB, no PyTorch dependency |
| Wake word | openWakeWord (ONNX) | Local, custom trainable, ~200KB models |
| Text injection | tmux send-keys | No focus needed, no permissions, works from anywhere |
| Audio lock | fcntl.flock | Kernel-managed, no stale locks on crash |
| MCP config per IDE | ~/.claude.json, ~/.cursor/mcp.json, ~/.codex/mcp.json | Each IDE has its own path |
| Voice rules injection | Sentinel-marked blocks | Idempotent updates, clean uninstall |
| Wake mic ownership | flock (non-blocking) | First session claims, others skip |
| Shell integration | Source line in .zshrc → shell-init.sh | Clean .zshrc, all logic in package |
| tmux transparency | Invisible config (no status bar, destroy-unattached) | User doesn't notice tmux |
| Wake phrase | "Hey listen" (universal) | One phrase, name routing for multi-session |
