# VoiceSmith MCP — TODO / Feature Tracker

## Status Legend
- 🔴 Not started
- 🟡 In progress / Set aside
- 🟢 Done
- ⚪ Deferred / Future

---

## Core Features

### Wake Word Flow
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 1 | 🟡 | Wake word detection (openWakeWord) | "Hey listen" model trained. v3 architecture (MCP message queue) designed but set aside — needs reliable text injection method |
| 2 | 🟢 | Cross-session mic lock | Only one session owns the wake mic (flock) |
| 3 | 🟡 | Text injection into sessions | tmux approach rejected (conflicts with terminal). v3 uses listen(mode="wake") + HTTP /wake_message — proven but set aside |
| 4 | 🟡 | Full flow test: wake → record → transcribe → deliver | v3 flow tested end-to-end. Set aside pending reliable injection |
| 5 | 🟢 | Wake enable/disable MCP tools | `wake_enable`, `wake_disable` implemented |
| 6 | 🟡 | Multi-session name routing | Code written, single-session tested. Multi-session untested. |

### Session Management
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 7 | 🟢 | Session registry (sessions.json) | With flock, stale PID cleanup, tmux_session field |
| 8 | 🟢 | Auto-assign different voice for new sessions | If name taken, picks next available Kokoro voice |
| 9 | 🟢 | Resume with previous voice | `set_voice` persists `last_voice_name`. On startup, server uses it as preferred name. |
| 10 | 🟢 | Cross-session audio lock (flock) | Prevents overlapping TTS playback |

### Menu Bar App
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 11 | 🟢 | Native SwiftUI menu bar app | VoiceSmith.app — session list, toggles, voice/model switching, health, updates |
| 12 | 🟢 | Session Activity window | Per-session sparkline graphs, health dots, voice picker, mute/test/stop controls |
| 13 | 🟢 | Orange pill listen indicator | Icon composited with NSImage — shows during active mic recording |
| 14 | 🟢 | Status dot on menu bar icon | Green (active), orange (idle), red (muted) |
| 15 | 🟢 | Auto-start at login | LaunchAgent with KeepAlive, auto-restarts on crash |
| 16 | 🟢 | Single instance guard | Kills duplicate processes on launch |
| 17 | 🟢 | Whisper model switcher | Inline download progress, auto-restart sessions |
| 18 | 🟢 | Audio device selection | Output (mpv) and input (sounddevice) dropdowns, live switching without restart |
| 19 | 🟢 | Update checker | HTTPS to npm registry, pinned version update |

### UX Improvements
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 20 | 🟢 | Session preheat / agent intro | AI calls `status` on first response, discovers assigned name, speaks intro |
| 21 | 🟢 | Tink ready sound | Plays after mic is live (not before) |
| 22 | 🟢 | Timeout nudge on listen | Configurable via `stt.nudge_on_timeout` (default off) |
| 23 | 🟢 | No-duplication voice rule | If speaking, don't write the same text |
| 24 | 🟢 | Session name verification | AI calls `status` to verify actual name instead of trusting hook |

### Installer
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 25 | 🟢 | Multi-IDE support (Claude Code, Cursor, Codex) | --claude, --cursor, --codex, --all flags |
| 26 | 🟢 | Personalized voice rules injection | Per-IDE, with voice picker, sentinel comments |
| 27 | 🟢 | Menu bar app compilation | Swift compilation, app bundle, codesign, LaunchAgent — both install.sh and npx |
| 28 | 🟢 | Uninstall support | install.sh --uninstall and npx voicesmith-mcp uninstall — removes LaunchAgents, config, rules, hooks |

### Bug Fixes
| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 29 | 🟢 | Config reset bug | voice_registry.save() was wiping config.json on race condition. Fixed: skip write on read failure + atomic writes |
| 30 | 🟢 | Session name hook mismatch | Hook guessed wrong name from sessions.json. Fixed: fallback queries /status for actual name |
| 31 | 🟢 | Periodic save thread not starting | _start_periodic_save_thread defined but never started the thread. Fixed. |
| 32 | 🟢 | Multi-client audio service | audio_service.c now broadcasts to all connected clients (was single-client) |

---

## Known Issues

| # | Status | Issue | Notes |
|---|--------|-------|-------|
| 33 | 🟡 | Wake word "Hey listen" model not detecting | Custom model scores near zero. Built-in "hey_jarvis" works at 0.99. Need to retrain the custom model. |
| 34 | 🔴 | Wake listener mic cleanup on crash | sounddevice stream may leak. OS should clean up on process death. |

---

## Future / Deferred

| # | Status | Feature | Notes |
|---|--------|---------|-------|
| 35 | ⚪ | Custom wake word training CLI | `npx voicesmith-mcp train-wake-word "Hey Nova"` — uses Colab or local training |
| 36 | ⚪ | GUI editor text injection (Cursor, VS Code) | Needs InputMethodKit, sendkeys, or editor extension |
| 37 | ⚪ | GitHub Actions CI | macOS runner, mocked tests (no models) |
| 38 | ⚪ | Streaming TTS playback | Play chunk 1 while synthesizing chunk 2 |
| 39 | ⚪ | Linux ready sound fallback | Bundled WAV + aplay/paplay |
| 40 | ⚪ | Conversation logging | Record all agent speech to transcript |
| 41 | ⚪ | "Hey listen, all" broadcast | Send to all sessions simultaneously |
| 42 | ⚪ | Windows support | fcntl→portalocker, mpv fallback, psutil for PID check, choco/scoop installer |
| 43 | ⚪ | Wake word sensitivity tuning | Per-environment threshold (noisy vs quiet) |
| 44 | ⚪ | SSE event stream | Replace polling with server-sent events for real-time menu bar updates |
| 45 | ⚪ | Dark mode icon variants | Auto-switch menu bar icon based on macOS appearance |

---

## Architecture Decisions (Recorded)

| Decision | Choice | Why |
|----------|--------|-----|
| TTS engine | Kokoro ONNX | Local, fast, 54 voices, ONNX Runtime |
| STT engine | faster-whisper | Local, accurate, same as OpenAI Whisper API |
| VAD | Silero VAD (ONNX) | Local, 2MB, no PyTorch dependency |
| Wake word | openWakeWord (ONNX) | Local, custom trainable, ~200KB models |
| Wake word delivery | MCP listen(mode="wake") + HTTP /wake_message | No terminal injection needed, works with any IDE |
| Audio lock | fcntl.flock | Kernel-managed, no stale locks on crash |
| Menu bar framework | SwiftUI (native) | ~5MB memory, SF Symbols, proper NSStatusItem, MenuBarExtra API |
| Audio device switching | Live config.json read on each play | Changes take effect without server restart |
| Config safety | Atomic writes (temp + rename), skip on read failure | Prevents config corruption from race conditions |
| MCP config per IDE | ~/.claude.json, ~/.cursor/mcp.json, ~/.codex/mcp.json | Each IDE has its own path |
| Voice rules injection | Sentinel-marked blocks | Idempotent updates, clean uninstall |
