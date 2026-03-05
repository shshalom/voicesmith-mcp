# VoiceSmith MCP — TODO / Feature Tracker

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
| 3 | 🟢 | tmux alias integration | shell-init.sh deployed, alias works transparently |
| 4 | 🟢 | Full flow test: wake → record → transcribe → tmux inject | Tested and working end-to-end |
| 5 | 🟢 | Wake enable/disable MCP tools | `wake_enable`, `wake_disable` implemented |
| 6 | 🟡 | Multi-session name routing | Code written, single-session tested. Multi-session name parsing untested. |

### Session Management
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 7 | 🟢 | Session registry (sessions.json) | With flock, stale PID cleanup, tmux_session field |
| 8 | 🟢 | Auto-assign different voice for new sessions | If name taken, picks next available Kokoro voice |
| 9 | 🟢 | Resume with previous voice | Fixed — `set_voice` persists `last_voice_name` to config.json. On startup, server uses it as preferred name instead of main_agent. |
| 10 | 🟢 | Cross-session audio lock (flock) | Prevents overlapping TTS playback |

### UX Improvements
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 11 | 🔴 | Menu bar indicator for wake listener | Show mic state (active/recording/disabled), click to toggle. Use `rumps` (Python) or native Swift. |
| 12 | 🟢 | Session preheat / agent intro | AI calls `status` on first response, discovers assigned name, speaks intro. Combined with name discovery. |
| 13 | 🟢 | Tink ready sound after wake word | Plays before recording starts |
| 14 | 🟢 | Timeout nudge on listen | Configurable via `stt.nudge_on_timeout` (default off). When enabled, speak_then_listen speaks "I didn't catch that." on timeout. |

### Installer
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 15 | 🟢 | Multi-IDE support (Claude Code, Cursor, Codex) | --claude, --cursor, --codex, --all flags |
| 16 | 🟢 | --with-voice-wake flag | Installs tmux, openwakeword, shell scripts, enables in config |
| 17 | 🟢 | Personalized voice rules injection | Per-IDE, with voice picker, sentinel comments |
| 18 | 🟢 | Deploy shell-init.sh and source line to .zshrc | Deployed and tested — alias works |

### Voice Rules / Behavior
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 19 | 🟢 | Questions trigger speak_then_listen | Mandatory for input questions, not rhetorical |
| 20 | 🟢 | Sub-agents pick Kokoro voice names | Documented in voice rules. Mandatory get_voice_registry call removed — agents pick names directly. |
| 21 | 🟢 | Speaking pattern (optional opening + closing voice) | Opening is optional (only when meaningful). Closing mandatory, block: true. |

---

## Known Issues

| # | Status | Issue | Notes |
|---|--------|-------|-------|
| 22 | 🟢 | Multiple sessions competing for mic | Fixed with wake mic flock |
| 23 | 🟢 | Stale sessions not cleaned up on crash | Fixed — periodic cleanup every 60s via existing save thread calls get_active_sessions() |
| 24 | 🟢 | tmux_session always null without alias | Fixed — alias sets VOICESMITH_TMUX env var |
| 25 | 🔴 | Wake listener doesn't release mic cleanly when MCP server crashes | flock handles it, but sounddevice stream may leak. OS should clean up on process death. |
| 26 | 🟢 | No audio cue when AI is listening (speak_then_listen) | Fixed — Tink plays before mic opens in listen(). Skipped for push-to-talk (has its own beep). |
| 27 | 🟢 | Low mic sensitivity / difficulty hearing user | Fixed — VAD threshold now configurable via config.json (stt.vad_threshold), default lowered from 0.5 to 0.3 |
| 28 | 🟢 | tmux may intercept Shift+Return (newline) | Fixed — added `extended-keys on` + `csi-u` format to tmux.conf. Also changed prefix from C-b to C-] to avoid key conflicts, added clipboard/focus/passthrough settings. |

---

## Future / Deferred

| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 29 | ⚪ | Custom wake word training CLI | `npx voicesmith-mcp train-wake-word "Hey Nova"` — uses Colab or local training |
| 30 | ⚪ | GUI editor support (Cursor, VS Code) | Needs InputMethodKit, sendkeys, or editor extension for text injection |
| 31 | 🟢 | README.md | Created with usage, troubleshooting, multi-session docs |
| 32 | 🟢 | LICENSE file | Apache 2.0 created |
| 33 | 🟢 | Publish to npm | Live at voicesmith-mcp on npm |
| 34 | ⚪ | GitHub Actions CI | macOS runner, mocked tests (no models) |
| 35 | ⚪ | Streaming TTS playback | Play chunk 1 while synthesizing chunk 2 |
| 36 | ⚪ | Linux ready sound fallback | Bundled WAV + aplay/paplay |
| 37 | ⚪ | Conversation logging | Record all agent speech to transcript |
| 38 | ⚪ | Larger Whisper models | Optional medium/large for higher accuracy |
| 39 | ⚪ | Visual wake indicator | Menu bar / notification when wake listener activates |
| 40 | ⚪ | "Hey listen, all" broadcast | Send to all sessions simultaneously |
| 41 | ⚪ | Windows support | Medium effort. Core engines (Kokoro ONNX, faster-whisper, Silero VAD, sounddevice) all work on Windows. Changes needed: (1) `fcntl.flock` → `portalocker` or `msvcrt.locking`, (2) audio playback fallback for Windows (mpv works via choco/scoop, need fallback), (3) ready sound from Windows system sounds or bundled WAV, (4) parent PID check via `psutil` instead of `ps`, (5) temp paths via `tempfile.gettempdir()`, (6) installer: `choco`/`scoop` instead of `brew`, venv path `Scripts/python.exe` |
| 42 | ⚪ | Wake word sensitivity tuning | Per-environment threshold (noisy vs quiet) |
| 43 | 🟢 | Periodic stale session cleanup | Runs every 60s via save thread, parent PID check for orphaned servers |

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
