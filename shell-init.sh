#!/bin/bash
# Agent Voice MCP — Shell initialization
# Sourced from ~/.zshrc or ~/.bashrc
# Only one line added to shell profile:
#   [ -f ~/.local/share/agent-voice-mcp/shell-init.sh ] && source ~/.local/share/agent-voice-mcp/shell-init.sh

# Only set up the alias if wake word feature is enabled
if [ -f "$HOME/.local/share/agent-voice-mcp/config.json" ]; then
    _agent_voice_wake_enabled=$(python3 -c "
import json
with open('$HOME/.local/share/agent-voice-mcp/config.json') as f:
    print(json.load(f).get('wake_word', {}).get('enabled', False))
" 2>/dev/null)

    if [ "$_agent_voice_wake_enabled" = "True" ]; then
        claude-voice() {
            if [ -n "$TMUX" ]; then
                # Already in tmux — just set the env var and run claude directly
                export AGENT_VOICE_TMUX=$(tmux display-message -p '#S')
                command claude "$@"
            else
                local session_name="agent-voice-$$"
                # Serialize args to temp file to preserve quoting
                local args_file
                args_file=$(mktemp /tmp/agent-voice-args.XXXXXX)
                printf '%s\0' "$@" > "$args_file"
                tmux -f "$HOME/.local/share/agent-voice-mcp/tmux.conf" \
                    new-session -s "$session_name" \
                    -e "AGENT_VOICE_TMUX=$session_name" \
                    -e "AGENT_VOICE_ARGS=$args_file" \
                    "$HOME/.local/share/agent-voice-mcp/tmux-launcher.sh"
            fi
        }
        alias claude='claude-voice'
    fi
    unset _agent_voice_wake_enabled
fi
