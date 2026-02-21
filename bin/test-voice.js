/**
 * Agent Voice MCP — Quick smoke test.
 *
 * Runs `python server.py --test` to verify TTS works.
 */

const { spawn } = require("child_process");
const {
  VENV_PYTHON,
  SERVER_PY,
  INSTALL_DIR,
  logHeader,
  logError,
  logOk,
  logInfo,
  fileExists,
  BOLD,
  RESET,
} = require("./utils");
const path = require("path");

async function run() {
  logHeader();
  console.log(`${BOLD}Running voice test...${RESET}\n`);

  const pythonPath = fileExists(VENV_PYTHON)
    ? VENV_PYTHON
    : null;

  const serverPath = fileExists(SERVER_PY)
    ? SERVER_PY
    : fileExists(path.join(process.cwd(), "server.py"))
    ? path.join(process.cwd(), "server.py")
    : null;

  if (!pythonPath) {
    logError("Python venv not found. Run 'npx agent-voice-mcp install' first.");
    process.exit(1);
  }

  if (!serverPath) {
    logError("server.py not found. Run 'npx agent-voice-mcp install' first.");
    process.exit(1);
  }

  // Run the smoke test
  const child = spawn(pythonPath, [serverPath, "--test"], {
    cwd: path.dirname(serverPath),
    stdio: "inherit",
  });

  child.on("close", (code) => {
    console.log("");
    if (code === 0) {
      logOk("Voice test passed!");
      logInfo('Start a Claude Code session to use voice tools.');
    } else {
      logError(`Voice test failed (exit code ${code})`);
      logInfo("Check that models are downloaded and espeak-ng is installed.");
    }
    console.log("");
  });
}

module.exports = { run };
