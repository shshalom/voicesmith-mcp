#!/bin/bash
# Agent Voice MCP — tmux launcher
# Reads serialized args from temp file and launches claude with proper quoting.
# Called by shell-init.sh inside a tmux session.

args=()
if [ -n "$AGENT_VOICE_ARGS" ] && [ -f "$AGENT_VOICE_ARGS" ]; then
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "$AGENT_VOICE_ARGS"
    rm -f "$AGENT_VOICE_ARGS"
fi

exec command claude "${args[@]}"
