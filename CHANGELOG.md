# Changelog

All notable changes to VoiceSmith MCP are documented here.

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
