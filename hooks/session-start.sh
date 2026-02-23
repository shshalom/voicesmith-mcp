#!/bin/bash
# Agent Voice MCP — SessionStart hook
# Discovers this session's assigned voice name and injects it as context.
# The AI then knows its name without relying on static CLAUDE.md rules.

SESSIONS_FILE="$HOME/.local/share/agent-voice-mcp/sessions.json"

# Read the hook input (not used yet, but available)
INPUT=$(cat)

# Find this session's MCP server by checking which port responds
SESSION_NAME=""
SESSION_VOICE=""

if [ -f "$SESSIONS_FILE" ]; then
    # Get all active sessions and find ours by PID ancestry
    # Since the MCP server is a child of this Claude Code process,
    # we check each session's HTTP status endpoint
    SESSION_INFO=$(python3 -c "
import json, os
try:
    with open('$SESSIONS_FILE') as f:
        data = json.load(f)
    # Find sessions with alive PIDs, prefer the one matching our tmux session
    tmux_session = os.environ.get('AGENT_VOICE_TMUX', '')
    for s in data.get('sessions', []):
        try:
            os.kill(s['pid'], 0)  # Check if alive
            if tmux_session and s.get('tmux_session') == tmux_session:
                print(f\"{s['name']}|{s['voice']}\")
                break
        except (OSError, ProcessLookupError):
            pass
    else:
        # No tmux match — use the most recent alive session
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

# If we found a session name, speak intro and inject context
if [ -n "$SESSION_NAME" ]; then
    # Find the session's HTTP port
    SESSION_PORT=$(python3 -c "
import json, os
try:
    with open('$SESSIONS_FILE') as f:
        data = json.load(f)
    for s in data.get('sessions', []):
        if s.get('name') == '$SESSION_NAME':
            try:
                os.kill(s['pid'], 0)
                print(s['port'])
                break
            except (OSError, ProcessLookupError):
                pass
except Exception:
    pass
" 2>/dev/null)

    # Speak the intro via HTTP /speak endpoint (uses already-loaded TTS)
    if [ -n "$SESSION_PORT" ]; then
        curl -s --max-time 10 -X POST "http://127.0.0.1:$SESSION_PORT/speak" \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"$SESSION_NAME\", \"text\": \"$SESSION_NAME here, ready to go.\"}" \
            >/dev/null 2>&1 &
    fi

    # Inject name as context for the AI
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
