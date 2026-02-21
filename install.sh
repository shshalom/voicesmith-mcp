#!/bin/bash
# Agent Voice MCP — Shell installer (alternative to npx)
# Usage: ./install.sh
set -e

BOLD="\033[1m"
GREEN="\033[32m"
BLUE="\033[34m"
YELLOW="\033[33m"
RED="\033[31m"
DIM="\033[2m"
RESET="\033[0m"

INSTALL_DIR="$HOME/.local/share/agent-voice-mcp"
MODEL_DIR="$INSTALL_DIR/models"
VENV_DIR="$INSTALL_DIR/.venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ok()     { echo -e "  ${GREEN}✓${RESET} $1"; }
action() { echo -ne "  ${BLUE}→${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET} $1"; }
err()    { echo -e "  ${RED}✗${RESET} $1"; }
info()   { echo -e "  ${DIM}ℹ${RESET} $1"; }

echo -e "\n${BOLD}🎙️  Agent Voice MCP — Local AI Voice System${RESET}\n"

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
for f in server.py shared.py config.py voice_registry.py requirements.txt; do
    [ -f "$SCRIPT_DIR/$f" ] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
done
for d in tts stt templates; do
    if [ -d "$SCRIPT_DIR/$d" ]; then
        mkdir -p "$INSTALL_DIR/$d"
        cp "$SCRIPT_DIR/$d/"* "$INSTALL_DIR/$d/"
    fi
done
ok "Server files copied to $INSTALL_DIR"

if [ -f "$VENV_DIR/bin/python3" ]; then
    ok "Existing venv found"
else
    action "Creating Python virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo -e "\r  ${GREEN}✓${RESET} Created venv at $VENV_DIR"
fi

action "Installing Python packages..."
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo -e "\r  ${GREEN}✓${RESET} All packages installed"

if [ ! -f "$INSTALL_DIR/config.json" ]; then
    [ -f "$SCRIPT_DIR/config.json" ] && cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
    ok "Default config.json created"
else
    ok "config.json already exists"
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

MCP_CONFIG="$HOME/.claude/mcp.json"
mkdir -p "$(dirname "$MCP_CONFIG")"

if [ -f "$MCP_CONFIG" ] && grep -q "agent-voice" "$MCP_CONFIG" 2>/dev/null; then
    ok "agent-voice already configured in mcp.json"
else
    # Create or merge mcp.json
    if [ -f "$MCP_CONFIG" ]; then
        # Use Python to merge JSON safely
        "$VENV_DIR/bin/python3" -c "
import json
with open('$MCP_CONFIG') as f:
    config = json.load(f)
config.setdefault('mcpServers', {})
config['mcpServers']['agent-voice'] = {
    'command': '$VENV_DIR/bin/python3',
    'args': ['$INSTALL_DIR/server.py']
}
with open('$MCP_CONFIG', 'w') as f:
    json.dump(config, f, indent=2)
"
    else
        cat > "$MCP_CONFIG" << MCPEOF
{
  "mcpServers": {
    "agent-voice": {
      "command": "$VENV_DIR/bin/python3",
      "args": ["$INSTALL_DIR/server.py"]
    }
  }
}
MCPEOF
    fi
    ok "Added agent-voice to ~/.claude/mcp.json"
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

VOICE_RULES="$HOME/.claude/agents/voice-rules.md"
mkdir -p "$(dirname "$VOICE_RULES")"

if [ -f "$VOICE_RULES" ]; then
    ok "voice-rules.md already exists"
else
    src="$INSTALL_DIR/templates/voice-rules.md"
    [ ! -f "$src" ] && src="$SCRIPT_DIR/templates/voice-rules.md"
    if [ -f "$src" ]; then
        cp "$src" "$VOICE_RULES"
        ok "Voice rules saved to ~/.claude/agents/voice-rules.md"
    else
        warn "voice-rules.md template not found"
    fi
fi

if [ -f "$HOME/.claude/CLAUDE.md" ]; then
    if grep -q "voice-rules" "$HOME/.claude/CLAUDE.md" 2>/dev/null; then
        ok "CLAUDE.md already references voice-rules.md"
    else
        info "Add to your CLAUDE.md: See also: ~/.claude/agents/voice-rules.md"
    fi
fi

# ─── Done ────────────────────────────────────────────────────────────────
echo -e "\n🎉 ${BOLD}Done!${RESET} Start a new Claude Code session to hear your AI speak."
echo -e '   Run "npx agent-voice-mcp test" to hear a sample voice.\n'
