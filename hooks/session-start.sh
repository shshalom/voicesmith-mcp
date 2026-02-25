#!/bin/bash
# VoiceSmith MCP — SessionStart hook
# 1. Receives session_id from Claude Code via stdin JSON
# 2. Sends session_id to the MCP server's POST /session endpoint
# 3. Server reconciles voice with sibling sessions (same session_id)
# 4. Returns the assigned voice name as additionalContext

SESSIONS_FILE="$HOME/.local/share/voicesmith-mcp/sessions.json"
INPUT=$(cat)

# Parse session_id from hook input (all hooks receive session_id via stdin)
SESSION_ID=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('session_id', ''))
except:
    pass
" 2>/dev/null)

SESSION_NAME=""
SESSION_VOICE=""

if [ -f "$SESSIONS_FILE" ]; then
    # Find this session's MCP server port (by PID liveness, prefer tmux match)
    PORT=$(python3 -c "
import json, os
try:
    with open('$SESSIONS_FILE') as f:
        data = json.load(f)
    tmux = os.environ.get('VOICESMITH_TMUX', '')
    # Try tmux match first
    for s in data.get('sessions', []):
        try:
            os.kill(s['pid'], 0)
            if tmux and s.get('tmux_session') == tmux:
                print(s['port'])
                raise SystemExit
        except (OSError, ProcessLookupError):
            pass
    # Prefer the most recent session without a session_id (just registered, waiting for hook)
    for s in reversed(data.get('sessions', [])):
        try:
            os.kill(s['pid'], 0)
            if not s.get('session_id'):
                print(s['port'])
                raise SystemExit
        except (OSError, ProcessLookupError):
            pass
    # Final fallback: most recent alive session
    for s in reversed(data.get('sessions', [])):
        try:
            os.kill(s['pid'], 0)
            print(s['port'])
            break
        except (OSError, ProcessLookupError):
            pass
except:
    pass
" 2>/dev/null)

    # Send session_id to the server if we have both port and session_id
    # Retry up to 3 times — the HTTP listener may not be ready yet
    if [ -n "$PORT" ] && [ -n "$SESSION_ID" ]; then
        RESPONSE=""
        for attempt in 1 2 3; do
            RESPONSE=$(curl -s --max-time 3 -X POST \
                -H "Content-Type: application/json" \
                -d "{\"session_id\": \"$SESSION_ID\"}" \
                "http://127.0.0.1:$PORT/session" 2>/dev/null)
            [ -n "$RESPONSE" ] && break
            sleep 0.5
        done

        if [ -n "$RESPONSE" ]; then
            SESSION_NAME=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    s = d.get('session', {})
    print(s.get('name', ''))
except:
    pass
" 2>/dev/null)
            SESSION_VOICE=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    s = d.get('session', {})
    print(s.get('voice', ''))
except:
    pass
" 2>/dev/null)
        fi
    fi

    # Fallback: read sessions.json directly if HTTP call didn't work
    if [ -z "$SESSION_NAME" ]; then
        SESSION_INFO=$(python3 -c "
import json, os
try:
    with open('$SESSIONS_FILE') as f:
        data = json.load(f)
    tmux = os.environ.get('VOICESMITH_TMUX', '')
    for s in data.get('sessions', []):
        try:
            os.kill(s['pid'], 0)
            if tmux and s.get('tmux_session') == tmux:
                print(f\"{s['name']}|{s['voice']}\")
                raise SystemExit
        except (OSError, ProcessLookupError):
            pass
    for s in reversed(data.get('sessions', [])):
        try:
            os.kill(s['pid'], 0)
            print(f\"{s['name']}|{s['voice']}\")
            break
        except (OSError, ProcessLookupError):
            pass
except:
    pass
" 2>/dev/null)

        if [ -n "$SESSION_INFO" ]; then
            SESSION_NAME=$(echo "$SESSION_INFO" | cut -d'|' -f1)
            SESSION_VOICE=$(echo "$SESSION_INFO" | cut -d'|' -f2)
        fi
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
