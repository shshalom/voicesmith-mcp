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
  MCP_CONFIG_USER,
  MCP_CONFIG_LEGACY,
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
    { path: MCP_CONFIG_USER, label: "MCP server registration in ~/.claude.json" },
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

  // Remove agent-voice entry from ~/.claude.json (preserve other config)
  if (fileExists(MCP_CONFIG_USER)) {
    const config = readMcpConfig();
    if (config.mcpServers && config.mcpServers["agent-voice"]) {
      delete config.mcpServers["agent-voice"];
      if (Object.keys(config.mcpServers).length === 0) {
        delete config.mcpServers;
      }
      // Only remove file if completely empty
      if (Object.keys(config).length === 0) {
        fs.unlinkSync(MCP_CONFIG_USER);
        logOk("Removed ~/.claude.json (was empty)");
      } else {
        writeMcpConfig(config);
        logOk("Removed agent-voice from ~/.claude.json");
      }
    } else {
      logInfo("agent-voice not in ~/.claude.json");
    }
  }

  // Also clean up legacy ~/.claude/mcp.json if it exists
  if (fileExists(MCP_CONFIG_LEGACY)) {
    try {
      const legacy = JSON.parse(fs.readFileSync(MCP_CONFIG_LEGACY, "utf8"));
      if (legacy.mcpServers && legacy.mcpServers["agent-voice"]) {
        delete legacy.mcpServers["agent-voice"];
        if (Object.keys(legacy.mcpServers).length === 0) {
          fs.unlinkSync(MCP_CONFIG_LEGACY);
          logOk("Cleaned up legacy ~/.claude/mcp.json");
        } else {
          fs.writeFileSync(MCP_CONFIG_LEGACY, JSON.stringify(legacy, null, 2) + "\n");
          logOk("Removed agent-voice from legacy ~/.claude/mcp.json");
        }
      }
    } catch { /* ignore */ }
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
