# Changelog

All notable changes to VoiceSmith MCP are documented here.

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
