/**
 * Agent Voice MCP — Uninstaller.
 *
 * Prompts for confirmation, then removes all installed components.
 */

const fs = require("fs");
const path = require("path");

const {
  INSTALL_DIR,
  MCP_CONFIG_LEGACY,
  VOICE_RULES_DEST,
  IDE_CONFIGS,
  logOk,
  logInfo,
  logWarn,
  logHeader,
  BOLD,
  RESET,
  RED,
  fileExists,
  dirExists,
  ask,
} = require("./utils");

async function run() {
  logHeader();
  console.log(`${BOLD}Uninstalling Agent Voice MCP...${RESET}\n`);

  // Detect which IDEs have the entry
  const configuredIdes = [];
  for (const [key, ide] of Object.entries(IDE_CONFIGS)) {
    if (fileExists(ide.configPath) && ide.hasEntry(ide.configPath)) {
      configuredIdes.push(key);
    }
  }

  // Show what will be removed
  console.log("The following will be removed:");
  console.log(`  ${dirExists(INSTALL_DIR) ? "•" : "○"} Server, venv, models, and config`);
  for (const key of configuredIdes) {
    console.log(`  • MCP entry in ${IDE_CONFIGS[key].name} (${IDE_CONFIGS[key].configPath})`);
  }
  if (configuredIdes.length === 0) {
    console.log(`  ○ No IDE MCP entries found`);
  }
  console.log(`  ${fileExists(VOICE_RULES_DEST) ? "•" : "○"} Voice rules template`);

  console.log(`\n${RED}This cannot be undone.${RESET}`);
  const answer = await ask("\nProceed with uninstall? (yes/no): ");

  if (answer.toLowerCase() !== "yes" && answer.toLowerCase() !== "y") {
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

  // Remove agent-voice entry from each IDE config
  for (const [key, ide] of Object.entries(IDE_CONFIGS)) {
    if (fileExists(ide.configPath)) {
      if (ide.removeEntry(ide.configPath)) {
        logOk(`Removed agent-voice from ${ide.name}`);
      }
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
