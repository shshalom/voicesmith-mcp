#!/bin/bash
# VoiceSmith MCP — Shell initialization
# Sourced from ~/.zshrc or ~/.bashrc
# Only one line added to shell profile:
#   [ -f ~/.local/share/voicesmith-mcp/shell-init.sh ] && source ~/.local/share/voicesmith-mcp/shell-init.sh

# Only set up the alias if wake word feature is enabled
if [ -f "$HOME/.local/share/voicesmith-mcp/config.json" ]; then
    _voicesmith_wake_enabled=$(python3 -c "
import json
with open('$HOME/.local/share/voicesmith-mcp/config.json') as f:
    print(json.load(f).get('wake_word', {}).get('enabled', False))
" 2>/dev/null)

    if [ "$_voicesmith_wake_enabled" = "True" ]; then
        claude-voice() {
            if [ -n "$TMUX" ]; then
                # Already in tmux — just set the env var and run claude directly
                export VOICESMITH_TMUX=$(tmux display-message -p '#S')
                command claude "$@"
            else
                local session_name="voicesmith-$$"
                # Serialize args to temp file to preserve quoting
                local args_file
                args_file=$(mktemp /tmp/voicesmith-args.XXXXXX)
                printf '%s\0' "$@" > "$args_file"
                tmux -f "$HOME/.local/share/voicesmith-mcp/tmux.conf" \
                    new-session -s "$session_name" \
                    -e "VOICESMITH_TMUX=$session_name" \
                    -e "VOICESMITH_ARGS=$args_file" \
                    "$HOME/.local/share/voicesmith-mcp/tmux-launcher.sh"
            fi
        }
        alias claude='claude-voice'
    fi
    unset _voicesmith_wake_enabled
fi
