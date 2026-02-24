/**
 * VoiceSmith MCP — Uninstaller.
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
  IDE_RULES,
  hasVoiceRulesBlock,
  removeAppendBlock,
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
  console.log(`${BOLD}Uninstalling VoiceSmith MCP...${RESET}\n`);

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

  // Remove voicesmith entry from each IDE config
  for (const [key, ide] of Object.entries(IDE_CONFIGS)) {
    if (fileExists(ide.configPath)) {
      if (ide.removeEntry(ide.configPath)) {
        logOk(`Removed voicesmith from ${ide.name}`);
      }
    }
  }

  // Clean up legacy ~/.claude/mcp.json if it exists
  if (fileExists(MCP_CONFIG_LEGACY)) {
    try {
      const legacy = JSON.parse(fs.readFileSync(MCP_CONFIG_LEGACY, "utf8"));
      if (legacy.mcpServers && legacy.mcpServers["voicesmith"]) {
        delete legacy.mcpServers["voicesmith"];
        if (Object.keys(legacy.mcpServers).length === 0) {
          fs.unlinkSync(MCP_CONFIG_LEGACY);
          logOk("Cleaned up legacy ~/.claude/mcp.json");
        } else {
          fs.writeFileSync(MCP_CONFIG_LEGACY, JSON.stringify(legacy, null, 2) + "\n");
          logOk("Removed voicesmith from legacy ~/.claude/mcp.json");
        }
      }
    } catch { /* ignore */ }
  }

  // Remove voice rules from IDE config files
  for (const [key, rule] of Object.entries(IDE_RULES)) {
    if (!fileExists(rule.path)) continue;

    if (rule.type === "file") {
      // Standalone file — just delete it
      fs.unlinkSync(rule.path);
      logOk(`Removed ${IDE_CONFIGS[key]?.name || key} voice rules`);
    } else if (rule.type === "append") {
      // Appended block — remove it from the file
      const content = fs.readFileSync(rule.path, "utf8");
      if (hasVoiceRulesBlock(content)) {
        const cleaned = removeAppendBlock(content);
        if (cleaned.trim().length === 0) {
          fs.unlinkSync(rule.path);
          logOk(`Removed ${rule.path} (was empty)`);
        } else {
          fs.writeFileSync(rule.path, cleaned);
          logOk(`Removed voice rules from ${IDE_CONFIGS[key]?.name || key}`);
        }
      }
    }
  }

  // Remove legacy voice-rules.md template
  if (fileExists(VOICE_RULES_DEST)) {
    fs.unlinkSync(VOICE_RULES_DEST);
    logOk("Removed voice-rules.md template");
  }

  // Remove wake word source line from shell profiles
  const os = require("os");
  const path = require("path");
  for (const profile of [
    path.join(os.homedir(), ".zshrc"),
    path.join(os.homedir(), ".bashrc"),
  ]) {
    if (fileExists(profile)) {
      const content = fs.readFileSync(profile, "utf8");
      if (content.includes("voicesmith-mcp")) {
        const cleaned = content
          .split("\n")
          .filter((line) => !line.includes("voicesmith-mcp"))
          .join("\n");
        fs.writeFileSync(profile, cleaned);
        logOk(`Removed source line from ${profile}`);
      }
    }
  }

  // Kill lingering tmux sessions
  try {
    const { execSync } = require("child_process");
    const sessions = execSync("tmux list-sessions -F '#{session_name}' 2>/dev/null", {
      encoding: "utf8",
    }).trim();
    for (const session of sessions.split("\n")) {
      if (session.startsWith("voicesmith-")) {
        execSync(`tmux kill-session -t '${session}' 2>/dev/null`);
        logOk(`Killed tmux session: ${session}`);
      }
    }
  } catch {
    // tmux not installed or no sessions — fine
  }

  console.log(
    `\n${BOLD}Uninstall complete.${RESET}`
  );
  console.log("");
}

module.exports = { run };
