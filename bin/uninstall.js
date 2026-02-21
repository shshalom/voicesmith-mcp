/**
 * Agent Voice MCP — Uninstaller.
 *
 * Prompts for confirmation, then removes all installed components.
 */

const fs = require("fs");
const path = require("path");
const readline = require("readline");

const {
  INSTALL_DIR,
  MCP_CONFIG,
  VOICE_RULES_DEST,
  logOk,
  logInfo,
  logWarn,
  logHeader,
  BOLD,
  RESET,
  RED,
  readMcpConfig,
  writeMcpConfig,
  fileExists,
  dirExists,
} = require("./utils");

function ask(question) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase());
    });
  });
}

async function run() {
  logHeader();
  console.log(`${BOLD}Uninstalling Agent Voice MCP...${RESET}\n`);

  // Show what will be removed
  const items = [
    { path: INSTALL_DIR, label: "Server, venv, models, and config" },
    { path: MCP_CONFIG, label: "MCP server registration (agent-voice entry)" },
    { path: VOICE_RULES_DEST, label: "Voice rules template" },
  ];

  console.log("The following will be removed:");
  for (const item of items) {
    const exists = fileExists(item.path) || dirExists(item.path);
    const status = exists ? "found" : "not found";
    console.log(`  ${exists ? "•" : "○"} ${item.label} (${status})`);
  }

  console.log(`\n${RED}This cannot be undone.${RESET}`);
  const answer = await ask("\nProceed with uninstall? (yes/no): ");

  if (answer !== "yes" && answer !== "y") {
    console.log("Uninstall cancelled.\n");
    return;
  }

  console.log("");

  // Remove install directory (venv, models, server files, config)
  if (dirExists(INSTALL_DIR)) {
    fs.rmSync(INSTALL_DIR, { recursive: true, force: true });
    logOk("Removed " + INSTALL_DIR);
  } else {
    logInfo("Install directory already absent");
  }

  // Remove agent-voice entry from mcp.json (preserve other servers)
  if (fileExists(MCP_CONFIG)) {
    const config = readMcpConfig();
    if (config.mcpServers && config.mcpServers["agent-voice"]) {
      delete config.mcpServers["agent-voice"];
      // If no servers left, remove the file
      if (Object.keys(config.mcpServers).length === 0 && Object.keys(config).length === 1) {
        fs.unlinkSync(MCP_CONFIG);
        logOk("Removed ~/.claude/mcp.json (was empty)");
      } else {
        writeMcpConfig(config);
        logOk("Removed agent-voice from ~/.claude/mcp.json");
      }
    } else {
      logInfo("agent-voice not in mcp.json");
    }
  }

  // Remove voice rules
  if (fileExists(VOICE_RULES_DEST)) {
    fs.unlinkSync(VOICE_RULES_DEST);
    logOk("Removed voice-rules.md");
  } else {
    logInfo("voice-rules.md already absent");
  }

  console.log(
    `\n${BOLD}Uninstall complete.${RESET}`
  );
  logInfo(
    "Your ~/.claude/CLAUDE.md was not modified. Remove voice rule references manually if needed."
  );
  console.log("");
}

module.exports = { run };
