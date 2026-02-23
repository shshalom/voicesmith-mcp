#!/bin/bash
# Agent Voice MCP — SessionStart hook (lightweight)
# Discovers this session's assigned voice name and injects it as context.
# No TTS calls — just reads sessions.json and returns the name.

SESSIONS_FILE="$HOME/.local/share/agent-voice-mcp/sessions.json"
INPUT=$(cat)

SESSION_NAME=""
SESSION_VOICE=""

if [ -f "$SESSIONS_FILE" ]; then
    SESSION_INFO=$(python3 -c "
import json, os
try:
    with open('$SESSIONS_FILE') as f:
        data = json.load(f)
    tmux_session = os.environ.get('AGENT_VOICE_TMUX', '')
    for s in data.get('sessions', []):
        try:
            os.kill(s['pid'], 0)
            if tmux_session and s.get('tmux_session') == tmux_session:
                print(f\"{s['name']}|{s['voice']}\")
                break
        except (OSError, ProcessLookupError):
            pass
    else:
        for s in reversed(data.get('sessions', [])):
            try:
                os.kill(s['pid'], 0)
                print(f\"{s['name']}|{s['voice']}\")
                break
            except (OSError, ProcessLookupError):
                pass
except Exception:
    pass
" 2>/dev/null)

    if [ -n "$SESSION_INFO" ]; then
        SESSION_NAME=$(echo "$SESSION_INFO" | cut -d'|' -f1)
        SESSION_VOICE=$(echo "$SESSION_INFO" | cut -d'|' -f2)
    fi
fi

if [ -n "$SESSION_NAME" ]; then
    cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Your assigned voice for this session is: ${SESSION_NAME} (voice: ${SESSION_VOICE}). Use name: \"${SESSION_NAME}\" for all speak calls."
  }
}
EOF
fi

exit 0
