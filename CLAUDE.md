# VoiceSmith MCP — Project Rules

## Editing Rules
- **Never edit installed output files directly** (e.g., `~/.claude/CLAUDE.md`, `~/.cursor/rules/voicesmith.mdc`, `~/.codex/AGENTS.md`).
- Always edit the **template source** at `templates/voice-rules.md` instead.
- The install script (`install.sh`) renders templates with `{{MAIN_AGENT}}` substitution and injects them into user config files. The installed copies are derived artifacts — the template is the source of truth.
- After editing a template, update the local installed copy to match (or re-run the installer) so the current session reflects the changes.
