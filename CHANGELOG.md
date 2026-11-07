# Changelog

All notable changes to VoiceSmith MCP are documented here.

## [Unreleased]

### Added
- **Engine service (shared model host) — ~90% memory reduction for multi-session:** New `engine_service.py` + `engine_client.py`. One daemon now hosts the Kokoro TTS and faster-whisper STT models ONCE; every MCP session becomes a thin socket client (~17 MB vs ~1.5 GB per session — with whisper-medium, sessions measured 1.4–1.8 GB each). Spawned on demand with a flock single-instance guard, exits after `engine.idle_exit_min` (default 30 min) idle, watches config.json and hot-reloads the STT model on model-size change. Same synthesize/transcribe/is_loaded interface, so SpeechQueue, listen, wake, and export_speech are unchanged. Kill switch: `engine.enabled: false` restores in-process models. `status` reports the daemon under `engine_service`. Live-verified: warm synthesis 0.19s for 1.4s of audio; medium-model transcription round-trips correctly.
- **Voice floor (multi-session audio arbitration):** New `voice_floor.py` — a kernel-managed flock that serializes ALL audio channel use (speaking *and* listening) across concurrent sessions. Fixes cross-talk when many agent threads run at once: one session's open mic no longer records another session's TTS as user speech, sessions no longer talk over each other, and `speak_then_listen` now holds the floor across the speak→listen gap so nothing barges in between question and answer. Waiters queue asynchronously and return a structured `floor_busy` error (with holder name/purpose/age) after `coordination.floor_wait` seconds (default 20). Crash-safe (flock auto-releases), reentrant per task, cancellable. The wake listener pauses while any session holds the floor, and wake-word recording takes it too. `status` reports the current holder. Disable via `coordination.enabled: false`.
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
