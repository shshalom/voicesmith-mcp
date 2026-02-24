#!/bin/bash
# VoiceSmith MCP — tmux launcher
# Reads serialized args from temp file and launches claude with proper quoting.
# Called by shell-init.sh inside a tmux session.

args=()
if [ -n "$VOICESMITH_ARGS" ] && [ -f "$VOICESMITH_ARGS" ]; then
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "$VOICESMITH_ARGS"
    rm -f "$VOICESMITH_ARGS"
fi

exec command claude "${args[@]}"
