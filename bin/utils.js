/**
 * Shared utilities for Agent Voice MCP installer CLI.
 */

const { execFile, exec } = require("child_process");
const { promisify } = require("util");
const path = require("path");
const fs = require("fs");
const os = require("os");

const execFileAsync = promisify(execFile);
const execAsync = promisify(exec);

// ─── Paths ───────────────────────────────────────────────────────────────────

const INSTALL_DIR = path.join(os.homedir(), ".local", "share", "agent-voice-mcp");
const MODEL_DIR = path.join(INSTALL_DIR, "models");
const VENV_DIR = path.join(INSTALL_DIR, ".venv");
const VENV_PYTHON = path.join(VENV_DIR, "bin", "python3");
const VENV_PIP = path.join(VENV_DIR, "bin", "pip");
const CONFIG_FILE = path.join(INSTALL_DIR, "config.json");
const SERVER_PY = path.join(INSTALL_DIR, "server.py");

const CLAUDE_AGENTS_DIR = path.join(os.homedir(), ".claude", "agents");
const VOICE_RULES_DEST = path.join(CLAUDE_AGENTS_DIR, "voice-rules.md");

// Source directory (where the npm package files live)
const PKG_DIR = path.resolve(__dirname, "..");

// Alternate model locations to check
const ALT_MODEL_DIRS = [
  path.join(os.homedir(), ".local", "share", "kokoro-tts", "models"),
];

// Model download URLs
const KOKORO_MODEL_URL =
  "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx";
const KOKORO_VOICES_URL =
  "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin";

// ─── IDE Registry ────────────────────────────────────────────────────────────

const IDE_CONFIGS = {
  claude: {
    name: "Claude Code",
    configPath: path.join(os.homedir(), ".claude.json"),
    detect: () => dirExists(path.join(os.homedir(), ".claude")),
    // Claude Code uses top-level mcpServers in ~/.claude.json
    read(configPath) {
      try {
        return JSON.parse(fs.readFileSync(configPath, "utf8"));
      } catch { return {}; }
    },
    write(configPath, data) {
      fs.writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
    },
    hasEntry(configPath) {
      const data = this.read(configPath);
      return !!(data.mcpServers && data.mcpServers["agent-voice"]);
    },
    addEntry(configPath) {
      const data = this.read(configPath);
      if (!data.mcpServers) data.mcpServers = {};
      data.mcpServers["agent-voice"] = { command: VENV_PYTHON, args: [SERVER_PY] };
      this.write(configPath, data);
    },
    removeEntry(configPath) {
      const data = this.read(configPath);
      if (data.mcpServers && data.mcpServers["agent-voice"]) {
        delete data.mcpServers["agent-voice"];
        if (Object.keys(data.mcpServers).length === 0) delete data.mcpServers;
        if (Object.keys(data).length === 0) {
          try { fs.unlinkSync(configPath); } catch {}
        } else {
          this.write(configPath, data);
        }
        return true;
      }
      return false;
    },
  },

  cursor: {
    name: "Cursor",
    configPath: path.join(os.homedir(), ".cursor", "mcp.json"),
    detect: () => dirExists(path.join(os.homedir(), ".cursor")),
    read(configPath) {
      try {
        return JSON.parse(fs.readFileSync(configPath, "utf8"));
      } catch { return {}; }
    },
    write(configPath, data) {
      ensureDir(path.dirname(configPath));
      fs.writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
    },
    hasEntry(configPath) {
      const data = this.read(configPath);
      return !!(data.mcpServers && data.mcpServers["agent-voice"]);
    },
    addEntry(configPath) {
      const data = this.read(configPath);
      if (!data.mcpServers) data.mcpServers = {};
      data.mcpServers["agent-voice"] = { command: VENV_PYTHON, args: [SERVER_PY] };
      this.write(configPath, data);
    },
    removeEntry(configPath) {
      const data = this.read(configPath);
      if (data.mcpServers && data.mcpServers["agent-voice"]) {
        delete data.mcpServers["agent-voice"];
        if (Object.keys(data.mcpServers).length === 0) {
          try { fs.unlinkSync(configPath); } catch {}
        } else {
          this.write(configPath, data);
        }
        return true;
      }
      return false;
    },
  },

  codex: {
    name: "Codex (OpenAI)",
    // Codex CLI reads from ~/.codex/mcp.json
    configPath: path.join(os.homedir(), ".codex", "mcp.json"),
    detect: () => dirExists(path.join(os.homedir(), ".codex")),
    read(configPath) {
      try {
        return JSON.parse(fs.readFileSync(configPath, "utf8"));
      } catch { return {}; }
    },
    write(configPath, data) {
      ensureDir(path.dirname(configPath));
      fs.writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n");
    },
    hasEntry(configPath) {
      const data = this.read(configPath);
      return !!(data.mcpServers && data.mcpServers["agent-voice"]);
    },
    addEntry(configPath) {
      const data = this.read(configPath);
      if (!data.mcpServers) data.mcpServers = {};
      data.mcpServers["agent-voice"] = { command: VENV_PYTHON, args: [SERVER_PY] };
      this.write(configPath, data);
    },
    removeEntry(configPath) {
      const data = this.read(configPath);
      if (data.mcpServers && data.mcpServers["agent-voice"]) {
        delete data.mcpServers["agent-voice"];
        if (Object.keys(data.mcpServers).length === 0) {
          try { fs.unlinkSync(configPath); } catch {}
        } else {
          this.write(configPath, data);
        }
        return true;
      }
      return false;
    },
  },
};

// Legacy path for cleanup
const MCP_CONFIG_LEGACY = path.join(os.homedir(), ".claude", "mcp.json");

// ─── IDE Flag Parsing ────────────────────────────────────────────────────────

function parseIdeFlags(argv) {
  const flags = [];
  for (const arg of argv) {
    if (arg === "--claude") flags.push("claude");
    else if (arg === "--cursor") flags.push("cursor");
    else if (arg === "--codex") flags.push("codex");
    else if (arg === "--all") flags.push("claude", "cursor", "codex");
  }
  return [...new Set(flags)];
}

function detectInstalledIdes() {
  const detected = [];
  for (const [key, ide] of Object.entries(IDE_CONFIGS)) {
    if (ide.detect()) {
      detected.push(key);
    }
  }
  return detected;
}

// ─── Logging ─────────────────────────────────────────────────────────────────

const GREEN = "\x1b[32m";
const BLUE = "\x1b[34m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const DIM = "\x1b[2m";
const RESET = "\x1b[0m";
const BOLD = "\x1b[1m";

function logOk(msg) {
  console.log(`  ${GREEN}✓${RESET} ${msg}`);
}
function logAction(msg) {
  process.stdout.write(`  ${BLUE}→${RESET} ${msg}`);
}
function logActionDone(msg) {
  process.stdout.write(`\r  ${GREEN}✓${RESET} ${msg}\n`);
}
function logWarn(msg) {
  console.log(`  ${YELLOW}⚠${RESET} ${msg}`);
}
function logError(msg) {
  console.log(`  ${RED}✗${RESET} ${msg}`);
}
function logInfo(msg) {
  console.log(`  ${DIM}ℹ${RESET} ${msg}`);
}
function logStep(n, total, msg) {
  console.log(`\n${BOLD}Step ${n}/${total}: ${msg}${RESET}`);
}
function logHeader() {
  console.log(`\n${BOLD}🎙️  Agent Voice MCP — Local AI Voice System${RESET}\n`);
}

// ─── Python Discovery ────────────────────────────────────────────────────────

const PYTHON_CANDIDATES = ["python3.12", "python3.11", "python3", "python"];

async function findPython() {
  for (const cmd of PYTHON_CANDIDATES) {
    try {
      const { stdout } = await execFileAsync(cmd, ["--version"]);
      const match = stdout.trim().match(/Python (\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1]);
        const minor = parseInt(match[2]);
        if (major === 3 && minor >= 11) {
          return { command: cmd, version: stdout.trim(), major, minor };
        }
      }
    } catch {
      // Command not found, try next
    }
  }
  return null;
}

// ─── Command Helpers ─────────────────────────────────────────────────────────

async function commandExists(cmd) {
  try {
    await execFileAsync("which", [cmd]);
    return true;
  } catch {
    return false;
  }
}

async function runCommand(cmd, args = [], opts = {}) {
  try {
    const { stdout, stderr } = await execFileAsync(cmd, args, {
      maxBuffer: 10 * 1024 * 1024,
      ...opts,
    });
    return { success: true, stdout, stderr };
  } catch (err) {
    return {
      success: false,
      stdout: err.stdout || "",
      stderr: err.stderr || err.message,
    };
  }
}

function fileExists(p) {
  try {
    fs.accessSync(p);
    return true;
  } catch {
    return false;
  }
}

function dirExists(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

// ─── Model Discovery ─────────────────────────────────────────────────────────

function findModel(filename) {
  // Check target location first
  const target = path.join(MODEL_DIR, filename);
  if (fileExists(target)) {
    return { path: target, location: "installed" };
  }

  // Check alternate locations
  for (const dir of ALT_MODEL_DIRS) {
    const alt = path.join(dir, filename);
    if (fileExists(alt)) {
      return { path: alt, location: dir };
    }
  }

  return null;
}

// ─── Readline Helper ─────────────────────────────────────────────────────────

function ask(question) {
  const rl = require("readline").createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim());
    });
  });
}

// ─── Exports ─────────────────────────────────────────────────────────────────

module.exports = {
  // Paths
  INSTALL_DIR,
  MODEL_DIR,
  VENV_DIR,
  VENV_PYTHON,
  VENV_PIP,
  CONFIG_FILE,
  SERVER_PY,
  CLAUDE_AGENTS_DIR,
  VOICE_RULES_DEST,
  PKG_DIR,
  ALT_MODEL_DIRS,
  KOKORO_MODEL_URL,
  KOKORO_VOICES_URL,
  MCP_CONFIG_LEGACY,

  // IDE
  IDE_CONFIGS,
  parseIdeFlags,
  detectInstalledIdes,

  // Logging
  logOk,
  logAction,
  logActionDone,
  logWarn,
  logError,
  logInfo,
  logStep,
  logHeader,
  GREEN,
  BLUE,
  YELLOW,
  RED,
  DIM,
  RESET,
  BOLD,

  // Helpers
  findPython,
  commandExists,
  runCommand,
  fileExists,
  dirExists,
  ensureDir,
  findModel,
  ask,
  execAsync,
};
