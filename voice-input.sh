#!/bin/bash
# Voice Input — triggered by macOS Voice Control "Hey listen"
#
# Reads active sessions, records speech via MCP server HTTP endpoint,
# parses the agent name from the first word, and pastes the text
# into the frontmost app.
#
# Usage: ./voice-input.sh
# Setup: Add "Hey listen" as a macOS Voice Control custom command
#        that runs this script via an Automator workflow.

set -euo pipefail

SESSIONS_FILE="$HOME/.local/share/agent-voice-mcp/sessions.json"
READY_SOUND="/System/Library/Sounds/Tink.aiff"

# ─── Helpers ──────────────────────────────────────────────────────────────────

notify() {
    osascript -e "display notification \"$1\" with title \"Agent Voice\""
}

# ─── Find Active Sessions ────────────────────────────────────────────────────

if [ ! -f "$SESSIONS_FILE" ]; then
    notify "No active voice session. Start a coding session first."
    exit 0
fi

# Parse sessions and filter alive PIDs
sessions=$(python3 -c "
import json, os
with open('$SESSIONS_FILE') as f:
    data = json.load(f)
alive = []
for s in data.get('sessions', []):
    try:
        os.kill(s['pid'], 0)
        alive.append(s)
    except (OSError, ProcessLookupError):
        pass
if not alive:
    print('NONE')
else:
    for s in alive:
        print(f\"{s['name']}:{s['port']}\")
" 2>/dev/null)

if [ "$sessions" = "NONE" ] || [ -z "$sessions" ]; then
    notify "No active voice session. Start a coding session first."
    exit 0
fi

# Count sessions
session_count=$(echo "$sessions" | wc -l | tr -d ' ')

# If only one session, use it directly
if [ "$session_count" -eq 1 ]; then
    target_name=$(echo "$sessions" | cut -d: -f1)
    target_port=$(echo "$sessions" | cut -d: -f2)
else
    # Multiple sessions — we'll record first, then parse the name
    # For now, use the first (most recently started) session to record
    # We'll parse the name from the transcription afterward
    target_port=$(echo "$sessions" | tail -1 | cut -d: -f2)
fi

# ─── Check Server Health ─────────────────────────────────────────────────────

health=$(curl -s --connect-timeout 2 "http://127.0.0.1:$target_port/status" 2>/dev/null || echo "")
if [ -z "$health" ]; then
    notify "Voice server not responding on port $target_port"
    exit 0
fi

# ─── Play Ready Sound ────────────────────────────────────────────────────────

if [ -f "$READY_SOUND" ]; then
    afplay "$READY_SOUND" &
fi

# ─── Record and Transcribe ───────────────────────────────────────────────────

response=$(curl -s --max-time 30 -X POST "http://127.0.0.1:$target_port/listen" 2>/dev/null || echo "")

if [ -z "$response" ]; then
    notify "Voice server did not respond"
    exit 0
fi

# Parse response
success=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success', False))" 2>/dev/null)
text=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('text', ''))" 2>/dev/null)
error=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error', ''))" 2>/dev/null)

if [ "$success" != "True" ]; then
    case "$error" in
        timeout) notify "No speech detected" ;;
        mic_busy) notify "Mic is busy — the AI is already listening" ;;
        muted) notify "Voice is muted" ;;
        *) notify "Error: $error" ;;
    esac
    exit 0
fi

if [ -z "$text" ]; then
    notify "No speech detected"
    exit 0
fi

# ─── Route to Session (multi-session name parsing) ───────────────────────────

if [ "$session_count" -gt 1 ]; then
    # Parse first word as potential session name
    first_word=$(echo "$text" | awk '{print $1}' | tr -d '.,!?:')
    rest_of_text=$(echo "$text" | sed "s/^[^ ]* *//")

    # Check if first word matches a session name (case-insensitive)
    matched_port=""
    while IFS= read -r session_line; do
        sname=$(echo "$session_line" | cut -d: -f1)
        sport=$(echo "$session_line" | cut -d: -f2)
        if [ "$(echo "$first_word" | tr '[:upper:]' '[:lower:]')" = "$(echo "$sname" | tr '[:upper:]' '[:lower:]')" ]; then
            matched_port="$sport"
            text="$rest_of_text"
            break
        fi
    done <<< "$sessions"

    # If no name matched, use the last (most recent) session
    if [ -z "$matched_port" ]; then
        # Keep full text, route to most recent
        :
    fi
fi

# ─── Inject Text into Frontmost App ─────────────────────────────────────────

if [ -n "$text" ]; then
    osascript -e "
        tell application \"System Events\"
            keystroke \"$text\"
            keystroke return
        end tell
    "
fi
