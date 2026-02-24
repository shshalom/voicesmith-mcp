#!/bin/bash
# VoiceSmith MCP — Shell installer (alternative to npx)
# Usage: ./install.sh [--claude] [--cursor] [--codex] [--all]
set -e

BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
DIM="\033[2m"
RESET="\033[0m"

INSTALL_DIR="$HOME/.local/share/voicesmith-mcp"
MODEL_DIR="$INSTALL_DIR/models"
VENV_DIR="$INSTALL_DIR/.venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SENTINEL="<!-- installed by voicesmith-mcp -->"

ok()     { echo -e "  ${GREEN}✓${RESET} $1"; }
action() { echo -ne "  ${BLUE}→${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET} $1"; }
err()    { echo -e "  ${RED}✗${RESET} $1"; }
info()   { echo -e "  ${DIM}ℹ${RESET} $1"; }

# ─── Parse flags ────────────────────────────────────────────────────────
TARGET_IDES=()

for arg in "$@"; do
    case "$arg" in
        --claude) TARGET_IDES+=("claude") ;;
        --cursor) TARGET_IDES+=("cursor") ;;
        --codex)  TARGET_IDES+=("codex") ;;
        --all)    TARGET_IDES=("claude" "cursor" "codex") ;;
    esac
done

echo -e "\n${BOLD}🎙️  VoiceSmith MCP — Local AI Voice System${RESET}\n"

# ─── Step 1: System deps ─────────────────────────────────────────────────
echo -e "\n${BOLD}Step 1/6: Checking system dependencies...${RESET}"

PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" = "3" ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.11+ not found. Install with: brew install python@3.12"
    exit 1
fi

full_version=$("$PYTHON" --version 2>&1)
if [ "$minor" -ge 13 ]; then
    warn "$full_version found (3.11 or 3.12 recommended)"
else
    ok "$full_version found"
fi

if command -v espeak-ng &>/dev/null; then
    ok "espeak-ng found"
else
    action "Installing espeak-ng..."
    brew install espeak-ng
    echo -e "\r  ${GREEN}✓${RESET} espeak-ng installed"
fi

if command -v mpv &>/dev/null; then
    ok "mpv found"
else
    action "Installing mpv..."
    brew install mpv
    echo -e "\r  ${GREEN}✓${RESET} mpv installed"
fi

# ─── Step 2: Python environment ──────────────────────────────────────────
echo -e "\n${BOLD}Step 2/6: Setting up Python environment...${RESET}"

mkdir -p "$INSTALL_DIR"

# Copy server files
for f in server.py shared.py config.py voice_registry.py session_registry.py requirements.txt; do
    [ -f "$SCRIPT_DIR/$f" ] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
done
for d in tts stt templates; do
    if [ -d "$SCRIPT_DIR/$d" ]; then
        mkdir -p "$INSTALL_DIR/$d"
        cp "$SCRIPT_DIR/$d/"* "$INSTALL_DIR/$d/"
    fi
done
ok "Server files copied to $INSTALL_DIR"

# Create or validate venv
if [ -f "$VENV_DIR/bin/python3" ]; then
    if "$VENV_DIR/bin/python3" --version &>/dev/null; then
        ok "Existing venv found ($("$VENV_DIR/bin/python3" --version 2>&1))"
    else
        warn "Existing venv is broken, recreating..."
        rm -rf "$VENV_DIR"
    fi
fi

if [ ! -f "$VENV_DIR/bin/python3" ]; then
    action "Creating Python virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo -e "\r  ${GREEN}✓${RESET} Created venv at $VENV_DIR"
fi

# Smart package install — only install what's missing
MISSING_PKGS=()
check_pkg() {
    local import_name="$1" pip_name="$2"
    if ! "$VENV_DIR/bin/python3" -c "import $import_name" &>/dev/null; then
        MISSING_PKGS+=("$pip_name")
    fi
}

check_pkg "kokoro_onnx" "kokoro-onnx"
check_pkg "faster_whisper" "faster-whisper"
check_pkg "soundfile" "soundfile"
check_pkg "sounddevice" "sounddevice"
check_pkg "mcp" "mcp[cli]"
check_pkg "numpy" "numpy"
check_pkg "silero_vad" "silero-vad"

if [ ${#MISSING_PKGS[@]} -eq 0 ]; then
    ok "All Python packages already installed"
else
    action "Installing ${MISSING_PKGS[*]}..."
    "$VENV_DIR/bin/pip" install --quiet "${MISSING_PKGS[@]}"
    echo -e "\r  ${GREEN}✓${RESET} All packages installed"
fi

# Create or merge config.json
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    [ -f "$SCRIPT_DIR/config.json" ] && cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
    ok "Default config.json created"
else
    # Merge new default keys into existing config without overwriting user values
    if [ -f "$SCRIPT_DIR/config.json" ]; then
        merge_result=$("$VENV_DIR/bin/python3" -c "
import json

with open('$INSTALL_DIR/config.json') as f:
    existing = json.load(f)
with open('$SCRIPT_DIR/config.json') as f:
    defaults = json.load(f)

updated = False
for key, val in defaults.items():
    if key not in existing:
        existing[key] = val
        updated = True
    elif isinstance(val, dict):
        for sub_key, sub_val in val.items():
            if sub_key not in existing[key]:
                existing[key][sub_key] = sub_val
                updated = True

if updated:
    with open('$INSTALL_DIR/config.json', 'w') as f:
        json.dump(existing, f, indent=2)
    print('updated')
else:
    print('current')
" 2>/dev/null)
        if [ "$merge_result" = "updated" ]; then
            ok "config.json updated with new defaults"
        else
            ok "config.json already up to date"
        fi
    else
        ok "config.json already exists"
    fi
fi

# ─── Step 3: Models ──────────────────────────────────────────────────────
echo -e "\n${BOLD}Step 3/6: Checking models...${RESET}"
mkdir -p "$MODEL_DIR"

check_model() {
    local filename="$1" url="$2" size="$3"
    local target="$MODEL_DIR/$filename"

    if [ -f "$target" ]; then
        ok "$filename already installed"
        return
    fi

    # Check alternate locations
    local alt="$HOME/.local/share/kokoro-tts/models/$filename"
    if [ -f "$alt" ]; then
        ln -sf "$alt" "$target" 2>/dev/null || cp "$alt" "$target"
        ok "$filename found at kokoro-tts (linked)"
        return
    fi

    action "Downloading $filename ($size)..."
    curl -L --progress-bar -o "$target" "$url"
    ok "$filename downloaded"
}

check_model "kokoro-v1.0.onnx" \
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" \
    "310MB"

check_model "voices-v1.0.bin" \
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" \
    "27MB"

info "whisper-base model (~150MB) will download automatically on first use"

# ─── Step 4: MCP config ─────────────────────────────────────────────────
echo -e "\n${BOLD}Step 4/6: Configuring MCP server...${RESET}"

# Auto-detect IDEs if no flags given
if [ ${#TARGET_IDES[@]} -eq 0 ]; then
    [ -d "$HOME/.claude" ] && TARGET_IDES+=("claude")
    [ -d "$HOME/.cursor" ] && TARGET_IDES+=("cursor")
    [ -d "$HOME/.codex" ]  && TARGET_IDES+=("codex")

    # If nothing detected, prompt
    if [ ${#TARGET_IDES[@]} -eq 0 ]; then
        echo -e "\n  Which IDE(s) are you using?"
        echo "    1) Claude Code"
        echo "    2) Cursor"
        echo "    3) Codex (OpenAI)"
        echo "    4) All of the above"
        echo ""
        read -rp "  Select (1-4, or comma-separated like 1,2): " ide_choice
        IFS=',' read -ra choices <<< "$ide_choice"
        for c in "${choices[@]}"; do
            c=$(echo "$c" | tr -d ' ')
            case "$c" in
                1) TARGET_IDES+=("claude") ;;
                2) TARGET_IDES+=("cursor") ;;
                3) TARGET_IDES+=("codex") ;;
                4) TARGET_IDES=("claude" "cursor" "codex") ;;
            esac
        done
        [ ${#TARGET_IDES[@]} -eq 0 ] && TARGET_IDES=("claude")
    else
        ide_names=""
        for ide in "${TARGET_IDES[@]}"; do
            case "$ide" in
                claude) ide_names+="Claude Code, " ;;
                cursor) ide_names+="Cursor, " ;;
                codex)  ide_names+="Codex, " ;;
            esac
        done
        info "Detected: ${ide_names%, }"
    fi
fi

# MCP server entry (same for all IDEs)
MCP_ENTRY="{\"command\": \"$VENV_DIR/bin/python3\", \"args\": [\"$INSTALL_DIR/server.py\"]}"

configure_mcp() {
    local config_path="$1" ide_name="$2"

    if [ -f "$config_path" ] && grep -q "voicesmith" "$config_path" 2>/dev/null; then
        ok "$ide_name: already configured"
        return
    fi

    mkdir -p "$(dirname "$config_path")"

    if [ -f "$config_path" ]; then
        "$VENV_DIR/bin/python3" -c "
import json
with open('$config_path') as f:
    config = json.load(f)
config.setdefault('mcpServers', {})
config['mcpServers']['voicesmith'] = {
    'command': '$VENV_DIR/bin/python3',
    'args': ['$INSTALL_DIR/server.py']
}
with open('$config_path', 'w') as f:
    json.dump(config, f, indent=2)
"
    else
        cat > "$config_path" << MCPEOF
{
  "mcpServers": {
    "voicesmith": {
      "command": "$VENV_DIR/bin/python3",
      "args": ["$INSTALL_DIR/server.py"]
    }
  }
}
MCPEOF
    fi
    ok "$ide_name: added to $config_path"
}

for ide in "${TARGET_IDES[@]}"; do
    case "$ide" in
        claude) configure_mcp "$HOME/.claude.json" "Claude Code" ;;
        cursor) configure_mcp "$HOME/.cursor/mcp.json" "Cursor" ;;
        codex)  configure_mcp "$HOME/.codex/mcp.json" "Codex" ;;
    esac
done

# Clean up legacy ~/.claude/mcp.json
LEGACY_MCP="$HOME/.claude/mcp.json"
if [ -f "$LEGACY_MCP" ] && grep -q "voicesmith" "$LEGACY_MCP" 2>/dev/null; then
    "$VENV_DIR/bin/python3" -c "
import json, os
with open('$LEGACY_MCP') as f:
    data = json.load(f)
if 'mcpServers' in data and 'voicesmith' in data['mcpServers']:
    del data['mcpServers']['voicesmith']
    if not data['mcpServers']:
        os.unlink('$LEGACY_MCP')
    else:
        with open('$LEGACY_MCP', 'w') as f:
            json.dump(data, f, indent=2)
" 2>/dev/null
    info "Cleaned up legacy ~/.claude/mcp.json"
fi

# ─── Step 5: Microphone ─────────────────────────────────────────────────
echo -e "\n${BOLD}Step 5/6: Checking microphone access...${RESET}"

if [ "$(uname)" = "Darwin" ]; then
    result=$("$VENV_DIR/bin/python3" -c "
import sounddevice as sd
try:
    s = sd.InputStream(samplerate=16000, channels=1, dtype='float32')
    s.start(); import time; time.sleep(0.1); s.stop(); s.close()
    print('ok')
except: print('fail')
" 2>/dev/null)

    if [ "$result" = "ok" ]; then
        ok "Microphone access granted"
    else
        warn "Could not verify microphone access. macOS may prompt on first use."
    fi
else
    ok "Microphone permission not required on this platform"
fi

# ─── Step 6: Voice rules ────────────────────────────────────────────────
echo -e "\n${BOLD}Step 6/6: Setting up voice rules...${RESET}"

# Voice picker
echo -e "\n  ${BOLD}Choose your main agent voice:${RESET}"
VOICES=(
    "am_eric:Eric:male, American, confident"
    "af_nova:Nova:female, American, clear"
    "am_onyx:Onyx:male, American, deep"
    "am_adam:Adam:male, American, neutral"
    "af_heart:Heart:female, American, warm"
    "am_fenrir:Fenrir:male, American, bold"
    "bf_emma:Emma:female, British, polished"
    "bm_george:George:male, British, classic"
)

for i in "${!VOICES[@]}"; do
    IFS=':' read -r vid vname vdesc <<< "${VOICES[$i]}"
    echo -e "    $((i+1))) $vname ${DIM}($vid — $vdesc)${RESET}"
done
echo -e "    $((${#VOICES[@]}+1))) Enter a custom voice ID"
echo ""

read -rp "  Select (1-$((${#VOICES[@]}+1))): " voice_choice
voice_choice=${voice_choice:-1}

if [ "$voice_choice" -ge 1 ] && [ "$voice_choice" -le "${#VOICES[@]}" ] 2>/dev/null; then
    IFS=':' read -r CHOSEN_VOICE_ID MAIN_AGENT _ <<< "${VOICES[$((voice_choice-1))]}"
elif [ "$voice_choice" = "$((${#VOICES[@]}+1))" ]; then
    read -rp "  Enter voice ID (e.g., af_bella): " CHOSEN_VOICE_ID
    # Capitalize the name part (after the prefix)
    MAIN_AGENT=$(echo "$CHOSEN_VOICE_ID" | sed 's/^[a-z]*_//' | awk '{print toupper(substr($0,1,1)) tolower(substr($0,2))}')
else
    # Default to Eric
    CHOSEN_VOICE_ID="am_eric"
    MAIN_AGENT="Eric"
fi

ok "Main agent voice: $MAIN_AGENT ($CHOSEN_VOICE_ID)"

# Update config.json with chosen voice
if [ -f "$INSTALL_DIR/config.json" ]; then
    "$VENV_DIR/bin/python3" -c "
import json
with open('$INSTALL_DIR/config.json') as f:
    config = json.load(f)
config['tts']['default_voice'] = '$CHOSEN_VOICE_ID'
config['main_agent'] = '$MAIN_AGENT'
with open('$INSTALL_DIR/config.json', 'w') as f:
    json.dump(config, f, indent=2)
" 2>/dev/null
fi

# Generate voice rules block from template
src="$INSTALL_DIR/templates/voice-rules.md"
[ ! -f "$src" ] && src="$SCRIPT_DIR/templates/voice-rules.md"

if [ -f "$src" ]; then
    RULES_BLOCK=$(sed "s/{{MAIN_AGENT}}/$MAIN_AGENT/g" "$src")
else
    warn "voice-rules.md template not found"
    RULES_BLOCK=""
fi

# Helper: inject sentinel-based rules into a file (append or replace)
inject_rules() {
    local target="$1" label="$2"
    mkdir -p "$(dirname "$target")"
    if [ -f "$target" ] && grep -q "$SENTINEL" "$target" 2>/dev/null; then
        "$VENV_DIR/bin/python3" -c "
import sys
sentinel = '$SENTINEL'
with open('$target') as f:
    content = f.read()
idx = content.find(sentinel)
before = content[:idx] if idx >= 0 else content
with open('$target', 'w') as f:
    f.write(before.rstrip() + '\n\n' + sentinel + '\n' + sys.stdin.read())
" <<< "$RULES_BLOCK"
        ok "$label voice rules updated"
    elif [ -f "$target" ]; then
        printf "\n%s\n%s\n" "$SENTINEL" "$RULES_BLOCK" >> "$target"
        ok "$label voice rules added"
    else
        printf "%s\n%s\n" "$SENTINEL" "$RULES_BLOCK" > "$target"
        ok "$label created with voice rules"
    fi
}

# Inject rules for each configured IDE
if [ -n "$RULES_BLOCK" ]; then
    for ide in "${TARGET_IDES[@]}"; do
        case "$ide" in
            claude)
                inject_rules "$HOME/.claude/CLAUDE.md" "Claude Code:"
                ;;
            cursor)
                CURSOR_RULE="$HOME/.cursor/rules/voicesmith.mdc"
                mkdir -p "$(dirname "$CURSOR_RULE")"
                cat > "$CURSOR_RULE" << CURSOREOF
---
description: Voice interaction rules for VoiceSmith MCP server
globs:
alwaysApply: true
---

$SENTINEL
# Voice Behavior Rules (VoiceSmith MCP)

$RULES_BLOCK
CURSOREOF
                ok "Cursor: voice rules written to $CURSOR_RULE"
                ;;
            codex)
                if [ -d "$HOME/.codex" ]; then
                    inject_rules "$HOME/.codex/AGENTS.md" "Codex:"
                fi
                ;;
        esac
    done
fi

# Copy hooks directory to install dir
if [ -d "$SCRIPT_DIR/hooks" ]; then
    mkdir -p "$INSTALL_DIR/hooks"
    cp "$SCRIPT_DIR/hooks/"* "$INSTALL_DIR/hooks/"
    chmod +x "$INSTALL_DIR/hooks/"*.sh 2>/dev/null
    ok "Hooks copied to $INSTALL_DIR/hooks"
fi

# Register SessionStart hook in Claude settings (for voice name discovery)
for ide in "${TARGET_IDES[@]}"; do
    if [ "$ide" = "claude" ]; then
        SETTINGS_FILE="$HOME/.claude/settings.json"
        HOOK_CMD="$INSTALL_DIR/hooks/session-start.sh"
        mkdir -p "$(dirname "$SETTINGS_FILE")"

        "$VENV_DIR/bin/python3" -c "
import json, os

settings_path = '$SETTINGS_FILE'
hook_cmd = '$HOOK_CMD'

settings = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}
if 'SessionStart' not in settings['hooks']:
    settings['hooks']['SessionStart'] = []

# Check if already registered
already = any(
    any(h.get('command', '').find('voicesmith-mcp') >= 0 for h in entry.get('hooks', []))
    for entry in settings['hooks']['SessionStart']
)

if not already:
    settings['hooks']['SessionStart'].append({
        'matcher': '',
        'hooks': [{'type': 'command', 'command': hook_cmd, 'timeout': 3}]
    })
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
    print('registered')
else:
    print('exists')
" 2>/dev/null

        hook_result=$?
        if [ "$hook_result" = "0" ]; then
            ok "Claude Code: SessionStart hook registered"
        fi
        break
    fi
done

# ─── Done ────────────────────────────────────────────────────────────────
ide_names=""
for ide in "${TARGET_IDES[@]}"; do
    case "$ide" in
        claude) ide_names+="Claude Code, " ;;
        cursor) ide_names+="Cursor, " ;;
        codex)  ide_names+="Codex, " ;;
    esac
done
ide_names=${ide_names%, }

echo -e "\n🎉 ${BOLD}Done!${RESET} Configured for: ${ide_names:-Claude Code}"
echo '   Restart your IDE session, then voice tools will be available.'
echo -e '   Run "npx voicesmith-mcp test" to hear a sample voice.\n'
