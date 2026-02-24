/**
 * VoiceSmith MCP — Configuration manager.
 *
 * Re-run voice picker / change settings.
 */

const fs = require("fs");
const readline = require("readline");
const {
  CONFIG_FILE,
  INSTALL_DIR,
  logHeader,
  logOk,
  logError,
  logInfo,
  fileExists,
  BOLD,
  RESET,
  DIM,
} = require("./utils");

const DEFAULT_VOICES = [
  { id: "am_eric", desc: "male, American, confident" },
  { id: "af_nova", desc: "female, American, clear" },
  { id: "am_onyx", desc: "male, American, deep" },
  { id: "am_adam", desc: "male, American, neutral" },
  { id: "af_heart", desc: "female, American, warm" },
  { id: "am_fenrir", desc: "male, American, bold" },
  { id: "bf_emma", desc: "female, British, polished" },
  { id: "bm_george", desc: "male, British, classic" },
];

function ask(question) {
  const rl = readline.createInterface({
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

async function run() {
  logHeader();
  console.log(`${BOLD}Configuration${RESET}\n`);

  if (!fileExists(CONFIG_FILE)) {
    logError("config.json not found. Run 'npx voicesmith-mcp install' first.");
    process.exit(1);
  }

  const config = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));

  console.log(`  Current settings:`);
  console.log(`    Default voice:  ${config.tts?.default_voice || "am_eric"}`);
  console.log(`    Main agent:     ${config.main_agent || "Eric"}`);
  console.log(`    STT model:      ${config.stt?.model_size || "base"}`);
  console.log(`    Audio player:   ${config.tts?.audio_player || "mpv"}`);
  console.log(`    Log level:      ${config.log_level || "info"}`);
  console.log("");

  // Voice picker
  console.log(`  ${BOLD}Choose a default voice:${RESET}`);
  DEFAULT_VOICES.forEach((v, i) => {
    const marker = v.id === config.tts?.default_voice ? " ←" : "";
    console.log(`    ${i + 1}) ${v.id} ${DIM}(${v.desc})${RESET}${marker}`);
  });
  console.log(`    ${DEFAULT_VOICES.length + 1}) Enter a custom voice ID`);
  console.log("");

  const choice = await ask("  Select (1-" + (DEFAULT_VOICES.length + 1) + "): ");
  const num = parseInt(choice);

  let newVoice;
  if (num >= 1 && num <= DEFAULT_VOICES.length) {
    newVoice = DEFAULT_VOICES[num - 1].id;
  } else if (num === DEFAULT_VOICES.length + 1) {
    newVoice = await ask("  Enter voice ID (e.g., af_bella): ");
  } else {
    console.log("  No change.\n");
    return;
  }

  if (newVoice) {
    config.tts.default_voice = newVoice;

    // Update main agent name from voice ID
    const name = newVoice.split("_").slice(1).join("_");
    const capitalized = name.charAt(0).toUpperCase() + name.slice(1);
    config.main_agent = capitalized;

    fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2) + "\n");
    logOk(`Default voice set to ${newVoice} (main agent: ${capitalized})`);
  }

  console.log("");
}

module.exports = { run };
