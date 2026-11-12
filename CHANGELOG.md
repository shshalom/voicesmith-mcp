# Changelog

All notable changes to VoiceSmith MCP are documented here.

## [Unreleased]

### Fixed
- **Volume slider "did nothing":** the menu bar's 1s poll overwrote the slider binding from state.json *while the user was dragging*, snapping it back to the old value before the release ever wrote the new one. Poll now skips the volume binding during a drag.
- **Live volume dead on slow audio devices:** the mpv IPC connection gave up after 1s, but mpv creates its socket only after audio-device init — a USB device (e.g. the B600 video bar) can exceed that window, silently disabling mid-playback volume. The connection is now established lazily inside the playback poll loop, retrying until the socket appears or playback ends.
- **"Mute All" didn't mute all:** the menu bar's Mute All POSTed to a snapshot of live sessions, so anything spawned after the click — sub-agents, new threads — kept talking. Mute-all is now a shared global flag (`state.json`) that every session checks on every audio operation, including sessions spawned after the flag was set. A global mute also suppresses listening, wake word detection, ready sounds, and preheat intros. Per-session mute is unchanged and still available.

### Added
- **Live in-flight mute and volume:** Mute All and the volume slider now act on the audio that is *currently playing*, not just the next utterance. During playback the player polls the shared state ~10x/s: a mute kills the player process within ~150 ms mid-sentence, a mute caught during synthesis produces zero leaked audio (pre-spawn check), and volume changes are pushed to mpv live over its IPC socket. A mute during `listen` closes the mic within ~0.5 s. Stopped utterances report `"stopped": true` to the agent. mpv is required for live volume (afplay/aplay fall back to per-chunk baked gain; the mute kill is live on all players).
- **Volume control for VoiceSmith:** global volume (menu bar slider, persists in `state.json`) plus per-session volume overrides (Session Activity slider, `POST /session_volume`, or the `set_volume` tool). Applied as sample gain, so it works even though agents speak through a dedicated output device — no more switching devices to adjust agent loudness. `set_volume(volume, scope="all")` lets any agent set the shared default; `status` reports everything under `mute` and `volume`.
- **Mute subagents:** global toggle (menu bar + `state.json`) that mutes agent-spawned sessions while the user's main threads keep talking. Subagent detection: T3 gives user-created threads plain UUID ids and agent-spawned children prefixed ids containing `:`; unbound sessions are treated as main threads.

### Added
- **Engine service (shared model host) — ~90% memory reduction for multi-session:** New `engine_service.py` + `engine_client.py`. One daemon now hosts the Kokoro TTS and faster-whisper STT models ONCE; every MCP session becomes a thin socket client (~17 MB vs ~1.5 GB per session — with whisper-medium, sessions measured 1.4–1.8 GB each). Spawned on demand with a flock single-instance guard, exits after `engine.idle_exit_min` (default 30 min) idle, watches config.json and hot-reloads the STT model on model-size change. Same synthesize/transcribe/is_loaded interface, so SpeechQueue, listen, wake, and export_speech are unchanged. Kill switch: `engine.enabled: false` restores in-process models. `status` reports the daemon under `engine_service`. Live-verified: warm synthesis 0.19s for 1.4s of audio; medium-model transcription round-trips correctly.
- **Registry self-wipe (root cause of the empty sessions.json):** T3 Code's seatbelt-sandboxed agent processes get `EPERM` from `os.kill(pid, 0)` when probing *other* sessions — `_pid_alive` read that as "dead", so every cleanup pass (each server's 60s periodic save) wiped the whole registry while all sessions were alive. `_pid_alive` now falls back to a `ps` liveness check on `EPERM`, and only *confirmed* absence (or a *confirmed* reparent to launchd, ppid == 1) removes a session; any failed probe keeps it. The orphan check also no longer treats a `ps` failure (previously `ppid=0`) as orphaned — one transient `ps` hiccup under load could mass-SIGTERM healthy sessions.
- **`set_thread` tool + HTTP `POST /t3_thread`:** Explicit, idempotent binding of a session to its T3 thread id — the reliable path when the harness doesn't inject `T3CODE_THREAD_ID` into spawned servers. Repairs a wiped registry entry from the session's own metadata, and stamps the id onto the floor sidecar (`set_default_extra`) so companions see it on the very next hold without a restart.
- **Voice floor (multi-session audio arbitration):** New `voice_floor.py` — a kernel-managed flock that serializes ALL audio channel use (speaking *and* listening) across concurrent sessions. Fixes cross-talk when many agent threads run at once: one session's open mic no longer records another session's TTS as user speech, sessions no longer talk over each other, and `speak_then_listen` now holds the floor across the speak→listen gap so nothing barges in between question and answer. Waiters queue asynchronously and return a structured `floor_busy` error (with holder name/purpose/age) after `coordination.floor_wait` seconds (default 20). Crash-safe (flock auto-releases), reentrant per task, cancellable. The wake listener pauses while any session holds the floor, and wake-word recording takes it too. `status` reports the current holder. Disable via `coordination.enabled: false`. The holder sidecar (`/tmp/voicesmith-floor.json`) now reflects **live audio phases**, not whole holds: `speak` = actual playback, `listen` = mic actually open (via the mic's on-ready callback), `synthesizing`/`transcribing`/`processing` for the in-between floor-held windows, `speak_then_listen`/`wake` as brief outer values. Companions (Codex Micro) overlay only on `speak`/`listen`. On T3 Code, the sidecar carries the session's `thread_id` (auto-captured from the harness-injected `T3CODE_THREAD_ID`, persisted in `sessions.json`). Voice rules now also ask agents to name themselves in voice questions ("Nova here — should I merge?").
- **`export_speech` tool:** Synthesize speech and write it to an audio file (e.g. WAV) instead of playing it. Same name/voice resolution and `speed` as `speak`; returns the output path, `duration_ms`, `synthesis_ms`, and `sample_rate`. Useful for building videos and screencasts, archiving narration, or batch TTS. Creates parent directories if missing.

### Fixed
- **Orphaned servers leaking memory:** If the launcher or MCP client was SIGKILLed, the Python server was reparented to launchd and kept its TTS/STT models resident (~2.3 GB each) indefinitely, so memory grew until reboot. A parent watchdog now exits the server within ~2 s of its parent disappearing.
- **Remaining SIGABRT-on-exit paths:** Every exit (stdin EOF, SIGTERM/SIGINT/SIGHUP, startup failure, any `sys.exit`/uncaught exception) now ends in `os._exit` after a single idempotent `_shutdown()`, so interpreter finalization never races the onnxruntime/ctranslate2 thread pools.
- **`.config-*.tmp` buildup:** Atomic config writes interrupted by a kill left empty temp files beside `config.json` (hundreds over months). Stale ones (>5 min old) are removed at startup.

### Changed
- **Audio service `ThrottleInterval`:** 0 → 2 s, so a crash-at-startup loop can't spin the CPU while still restarting promptly.

## [1.0.18] - 2026-03-05

### Added
- **`stt.nudge_on_timeout` config:** Configurable spoken nudge when `speak_then_listen` times out. Default: off. When enabled, speaks "I didn't catch that. Go ahead and type it." before falling back to text.

### Changed
- **SPEC.md:** Updated `speak_then_listen` timeout docs, config.json template (added `duck_media`, `nudge_on_timeout`), media ducking description, mic flush timing (200ms → 64ms), ready sound plays after mic is live
- **SPEC-push-to-talk.md:** Updated nudge references to reflect configurable toggle

## [1.0.17] - 2026-03-05

### Fixed
- **Mic capture:** Fix losing first seconds of speech — improved stream startup and buffer handling
- **TTS:** Pad tail silence to prevent audio clipping at end of speech
- **Listen timeout:** Remove spoken nudge on timeout — fall back to text silently

### Added
- **Media ducking:** Auto-lower system media volume during TTS playback, with Bluetooth-aware delays
- **`tts/media_duck.py`:** New module for macOS media ducking via AppleScript

## [1.0.14] - 2026-02-28

### Changed
- **`set_voice` tool:** Now also renames the session so name and voice always match. Name is derived from voice ID (e.g., `am_fenrir` → "Fenrir"). Returns `previous_name` when renamed. Returns `name_occupied` error if the target name is taken by another session.

### Added
- **`voice_registry.py`:** `rename_voice()` method — atomically swaps old/new name entries in the registry
- **`session_registry.py`:** `rename_session()` function — renames a session in sessions.json with conflict detection

## [1.0.10] - 2026-02-28

### Changed
- **Voice rules template:** Opening speak is now optional — only speak when meaningful, no filler acknowledgments
- **Voice rules template:** Clearer question heuristic — use `speak_then_listen` only when the AI literally cannot continue without user input
- **Voice rules template:** Sentence limit relaxed from strict "1-2" to "prefer 1-2, never exceed 3"
- **Voice rules template:** Removed mandatory `get_voice_registry` call before sub-agent voice assignment
- **SPEC.md:** Updated Rules Content section and background description to match new voice rules

### Added
- **Voice rules template:** Tone guidance — be conversational, match the user's energy
- **Voice rules template:** Speed preferences section — document `speed` parameter, remember user preference
- **Voice rules template:** Error handling section — fall back to text silently on TTS/STT failure
- **Project CLAUDE.md:** Added project-level rules (always edit templates, never installed output files)
