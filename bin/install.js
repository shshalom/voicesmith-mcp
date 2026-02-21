/**
 * Agent Voice MCP — Interactive installer (6 steps).
 *
 * Smart detection: checks for existing tools, models, and configs.
 * Only installs/downloads what's actually missing.
 */

const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");
const { createWriteStream } = require("fs");
const readline = require("readline");

const {
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
  KOKORO_MODEL_URL,
  KOKORO_VOICES_URL,
  MCP_CONFIG_LEGACY,
  IDE_CONFIGS,
  parseIdeFlags,
  detectInstalledIdes,
  logOk,
  logAction,
  logActionDone,
  logWarn,
  logError,
  logInfo,
  logStep,
  logHeader,
  BOLD,
  RESET,
  DIM,
  findPython,
  commandExists,
  runCommand,
  fileExists,
  dirExists,
  ensureDir,
  findModel,
  ask,
} = require("./utils");

const TOTAL_STEPS = 6;

// ─── Step 1: System Dependencies ─────────────────────────────────────────────

async function step1_systemDeps() {
  logStep(1, TOTAL_STEPS, "Checking system dependencies...");

  // Python
  const python = await findPython();
  if (!python) {
    logError("Python 3.11+ not found");
    logInfo('Install with: brew install python@3.12');
    process.exit(1);
  }

  if (python.minor >= 13) {
    logWarn(`${python.version} found (3.11 or 3.12 recommended for best compatibility)`);
  } else {
    logOk(`${python.version} found`);
  }

  // espeak-ng
  if (await commandExists("espeak-ng")) {
    logOk("espeak-ng found");
  } else {
    logAction("Installing espeak-ng via Homebrew...");
    const result = await runCommand("brew", ["install", "espeak-ng"]);
    if (result.success) {
      logActionDone("espeak-ng installed");
    } else {
      logError("Failed to install espeak-ng. Install manually: brew install espeak-ng");
      process.exit(1);
    }
  }

  // mpv
  if (await commandExists("mpv")) {
    logOk("mpv found");
  } else {
    logAction("Installing mpv via Homebrew...");
    const result = await runCommand("brew", ["install", "mpv"]);
    if (result.success) {
      logActionDone("mpv installed");
    } else {
      logError("Failed to install mpv. Install manually: brew install mpv");
      process.exit(1);
    }
  }

  return python;
}

// ─── Step 2: Python Environment ──────────────────────────────────────────────

async function step2_pythonEnv(python) {
  logStep(2, TOTAL_STEPS, "Setting up Python environment...");

  ensureDir(INSTALL_DIR);

  // Copy server files from npm package to install dir
  const serverFiles = [
    "server.py",
    "shared.py",
    "config.py",
    "voice_registry.py",
    "requirements.txt",
  ];
  const serverDirs = ["tts", "stt", "templates"];

  for (const file of serverFiles) {
    const src = path.join(PKG_DIR, file);
    const dest = path.join(INSTALL_DIR, file);
    if (fileExists(src)) {
      fs.copyFileSync(src, dest);
    }
  }

  for (const dir of serverDirs) {
    const srcDir = path.join(PKG_DIR, dir);
    const destDir = path.join(INSTALL_DIR, dir);
    if (dirExists(srcDir)) {
      ensureDir(destDir);
      for (const file of fs.readdirSync(srcDir)) {
        if (file === "__pycache__" || file.endsWith(".pyc")) continue;
        const srcPath = path.join(srcDir, file);
        if (fs.statSync(srcPath).isFile()) {
          fs.copyFileSync(srcPath, path.join(destDir, file));
        }
      }
    }
  }
  logOk(`Server files copied to ${INSTALL_DIR}`);

  // Check if venv already exists and works
  if (fileExists(VENV_PYTHON)) {
    const check = await runCommand(VENV_PYTHON, ["--version"]);
    if (check.success) {
      logOk(`Existing venv found (${check.stdout.trim()})`);
    } else {
      logWarn("Existing venv is broken, recreating...");
      fs.rmSync(VENV_DIR, { recursive: true, force: true });
    }
  }

  // Create venv if needed
  if (!fileExists(VENV_PYTHON)) {
    logAction("Creating Python virtual environment...");
    const result = await runCommand(python.command, ["-m", "venv", VENV_DIR]);
    if (!result.success) {
      logError(`Failed to create venv: ${result.stderr}`);
      process.exit(1);
    }
    logActionDone(`Created venv at ${VENV_DIR}`);
  }

  // Check which packages are missing
  const requiredImports = [
    ["kokoro_onnx", "kokoro-onnx"],
    ["faster_whisper", "faster-whisper"],
    ["soundfile", "soundfile"],
    ["sounddevice", "sounddevice"],
    ["mcp", "mcp[cli]"],
    ["numpy", "numpy"],
    ["torch", "torch"],
  ];

  const missing = [];
  for (const [importName, pipName] of requiredImports) {
    const check = await runCommand(VENV_PYTHON, [
      "-c",
      `import ${importName}`,
    ]);
    if (!check.success) {
      missing.push(pipName);
    }
  }

  if (missing.length === 0) {
    logOk("All Python packages already installed");
  } else {
    logAction(`Installing ${missing.join(", ")}...`);
    const result = await runCommand(VENV_PIP, [
      "install",
      "--quiet",
      ...missing,
    ]);
    if (result.success) {
      logActionDone("All packages installed");
    } else {
      logError(`Package install failed: ${result.stderr}`);
      process.exit(1);
    }
  }

  // Copy default config if not present
  if (!fileExists(CONFIG_FILE)) {
    const src = path.join(PKG_DIR, "config.json");
    if (fileExists(src)) {
      fs.copyFileSync(src, CONFIG_FILE);
      logOk("Default config.json created");
    }
  } else {
    logOk("config.json already exists");
  }
}

// ─── Step 3: Models ──────────────────────────────────────────────────────────

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const follow = (url) => {
      https
        .get(url, (res) => {
          if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
            follow(res.headers.location);
            return;
          }
          if (res.statusCode !== 200) {
            reject(new Error(`HTTP ${res.statusCode} downloading ${url}`));
            return;
          }

          const total = parseInt(res.headers["content-length"] || "0");
          let downloaded = 0;
          const file = createWriteStream(dest);

          res.on("data", (chunk) => {
            downloaded += chunk.length;
            if (total > 0) {
              const pct = Math.round((downloaded / total) * 100);
              const mb = (downloaded / 1024 / 1024).toFixed(0);
              const totalMb = (total / 1024 / 1024).toFixed(0);
              process.stdout.write(
                `\r  → Downloading... ${mb}MB / ${totalMb}MB (${pct}%)`
              );
            }
          });

          res.pipe(file);
          file.on("finish", () => {
            file.close();
            process.stdout.write("\n");
            resolve();
          });
          file.on("error", reject);
        })
        .on("error", reject);
    };
    follow(url);
  });
}

async function step3_models() {
  logStep(3, TOTAL_STEPS, "Checking models...");
  ensureDir(MODEL_DIR);

  const models = [
    {
      filename: "kokoro-v1.0.onnx",
      url: KOKORO_MODEL_URL,
      size: "310MB",
    },
    {
      filename: "voices-v1.0.bin",
      url: KOKORO_VOICES_URL,
      size: "27MB",
    },
  ];

  for (const model of models) {
    const targetPath = path.join(MODEL_DIR, model.filename);

    if (fileExists(targetPath)) {
      logOk(`${model.filename} already installed`);
      continue;
    }

    // Check alternate locations
    const found = findModel(model.filename);
    if (found) {
      // Symlink from existing location
      try {
        fs.symlinkSync(found.path, targetPath);
        logOk(
          `${model.filename} found at ${found.location} (symlinked)`
        );
        continue;
      } catch {
        // Symlink failed, try copy
        fs.copyFileSync(found.path, targetPath);
        logOk(
          `${model.filename} found at ${found.location} (copied)`
        );
        continue;
      }
    }

    // Download
    logAction(`Downloading ${model.filename} (${model.size})...`);
    try {
      await downloadFile(model.url, targetPath);
      logOk(`${model.filename} downloaded`);
    } catch (err) {
      logError(`Failed to download ${model.filename}: ${err.message}`);
      process.exit(1);
    }
  }

  logInfo("whisper-base model (~150MB) will download automatically on first use");
}

// ─── Step 4: MCP Config (IDE picker) ─────────────────────────────────────────

async function step4_mcpConfig(targetIdes) {
  logStep(4, TOTAL_STEPS, "Configuring MCP server...");

  let ides = targetIdes;

  // If no IDEs specified via flags, detect and prompt
  if (!ides || ides.length === 0) {
    const detected = detectInstalledIdes();

    if (detected.length === 0) {
      // Nothing detected — ask which to configure
      console.log(`\n  Which IDE(s) are you using?`);
      console.log(`    1) Claude Code`);
      console.log(`    2) Cursor`);
      console.log(`    3) Codex (OpenAI)`);
      console.log(`    4) All of the above`);
      console.log("");

      const choice = await ask("  Select (1-4, or comma-separated like 1,2): ");
      const nums = choice.split(",").map((s) => parseInt(s.trim()));
      ides = [];
      for (const n of nums) {
        if (n === 1) ides.push("claude");
        if (n === 2) ides.push("cursor");
        if (n === 3) ides.push("codex");
        if (n === 4) ides = ["claude", "cursor", "codex"];
      }
      if (ides.length === 0) ides = ["claude"]; // default
    } else {
      // Auto-configure detected IDEs
      ides = detected;
      logInfo(`Detected: ${detected.map((k) => IDE_CONFIGS[k].name).join(", ")}`);
    }
  }

  // Configure each IDE
  for (const key of ides) {
    const ide = IDE_CONFIGS[key];
    if (!ide) {
      logWarn(`Unknown IDE: ${key}`);
      continue;
    }

    if (ide.hasEntry(ide.configPath)) {
      logOk(`${ide.name}: already configured`);
    } else {
      ide.addEntry(ide.configPath);
      logOk(`${ide.name}: added to ${ide.configPath}`);
    }
  }

  // Clean up legacy ~/.claude/mcp.json if it exists
  if (fileExists(MCP_CONFIG_LEGACY)) {
    try {
      const legacy = JSON.parse(fs.readFileSync(MCP_CONFIG_LEGACY, "utf8"));
      if (legacy.mcpServers && legacy.mcpServers["agent-voice"]) {
        delete legacy.mcpServers["agent-voice"];
        if (Object.keys(legacy.mcpServers).length === 0) {
          fs.unlinkSync(MCP_CONFIG_LEGACY);
        } else {
          fs.writeFileSync(MCP_CONFIG_LEGACY, JSON.stringify(legacy, null, 2) + "\n");
        }
        logInfo("Cleaned up legacy ~/.claude/mcp.json");
      }
    } catch { /* ignore */ }
  }

  return ides;
}

// ─── Step 5: Microphone ─────────────────────────────────────────────────────

async function step5_microphone() {
  logStep(5, TOTAL_STEPS, "Checking microphone access...");

  if (process.platform !== "darwin") {
    logOk("Microphone permission not required on this platform");
    return;
  }

  // On macOS, trigger the permission dialog by briefly opening sounddevice
  const result = await runCommand(VENV_PYTHON, [
    "-c",
    `
import sounddevice as sd
try:
    stream = sd.InputStream(samplerate=16000, channels=1, dtype='float32')
    stream.start()
    import time; time.sleep(0.1)
    stream.stop()
    stream.close()
    print("ok")
except Exception as e:
    print(f"error: {e}")
`,
  ]);

  if (result.success && result.stdout.trim() === "ok") {
    logOk("Microphone access granted");
  } else {
    logWarn(
      "Could not verify microphone access. macOS may prompt on first use."
    );
  }
}

// ─── Step 6: Voice Rules ────────────────────────────────────────────────────

async function step6_voiceRules() {
  logStep(6, TOTAL_STEPS, "Setting up voice rules...");

  ensureDir(CLAUDE_AGENTS_DIR);

  // Copy voice-rules.md template
  if (fileExists(VOICE_RULES_DEST)) {
    logOk("voice-rules.md already exists");
  } else {
    const src = path.join(PKG_DIR, "templates", "voice-rules.md");
    const installSrc = path.join(INSTALL_DIR, "templates", "voice-rules.md");
    const source = fileExists(src) ? src : fileExists(installSrc) ? installSrc : null;

    if (source) {
      fs.copyFileSync(source, VOICE_RULES_DEST);
      logOk("Voice rules saved to ~/.claude/agents/voice-rules.md");
    } else {
      logWarn("voice-rules.md template not found — skipping");
    }
  }

  // Check if CLAUDE.md references voice-rules
  const claudeMd = path.join(os.homedir(), ".claude", "CLAUDE.md");
  if (fileExists(claudeMd)) {
    const content = fs.readFileSync(claudeMd, "utf8");
    if (content.includes("voice-rules")) {
      logOk("CLAUDE.md already references voice-rules.md");
    } else {
      logInfo(
        'Add this to your CLAUDE.md: See also: ~/.claude/agents/voice-rules.md'
      );
    }
  } else {
    logInfo("No ~/.claude/CLAUDE.md found. Create one and reference voice-rules.md");
  }
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function run() {
  logHeader();

  // Parse --claude, --cursor, --codex, --all flags from argv
  const targetIdes = parseIdeFlags(process.argv.slice(3));

  const python = await step1_systemDeps();
  await step2_pythonEnv(python);
  await step3_models();
  const configuredIdes = await step4_mcpConfig(targetIdes);
  await step5_microphone();
  await step6_voiceRules();

  const ideNames = (configuredIdes || [])
    .map((k) => IDE_CONFIGS[k]?.name || k)
    .join(", ");

  console.log(
    `\n🎉 ${BOLD}Done!${RESET} Configured for: ${ideNames || "Claude Code"}`
  );
  console.log('   Restart your IDE session, then voice tools will be available.');
  console.log('   Run "npx agent-voice-mcp test" to hear a sample voice.\n');
}

module.exports = { run };
