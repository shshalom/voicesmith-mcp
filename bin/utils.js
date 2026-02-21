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

const MCP_CONFIG = path.join(os.homedir(), ".claude", "mcp.json");
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

// ─── MCP Config ──────────────────────────────────────────────────────────────

function readMcpConfig() {
  try {
    return JSON.parse(fs.readFileSync(MCP_CONFIG, "utf8"));
  } catch {
    return {};
  }
}

function writeMcpConfig(config) {
  ensureDir(path.dirname(MCP_CONFIG));
  fs.writeFileSync(MCP_CONFIG, JSON.stringify(config, null, 2) + "\n");
}

function hasMcpEntry() {
  const config = readMcpConfig();
  return !!(config.mcpServers && config.mcpServers["agent-voice"]);
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
  MCP_CONFIG,
  CLAUDE_AGENTS_DIR,
  VOICE_RULES_DEST,
  PKG_DIR,
  ALT_MODEL_DIRS,
  KOKORO_MODEL_URL,
  KOKORO_VOICES_URL,

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
  readMcpConfig,
  writeMcpConfig,
  hasMcpEntry,
  execAsync,
};
