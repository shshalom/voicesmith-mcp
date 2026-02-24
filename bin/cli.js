#!/usr/bin/env node

/**
 * VoiceSmith MCP — CLI entry point.
 *
 * Usage:
 *   npx voicesmith-mcp install     Full interactive setup
 *   npx voicesmith-mcp test        Play a sample voice to verify
 *   npx voicesmith-mcp voices      Browse and preview all voices
 *   npx voicesmith-mcp config      Re-run voice picker / change settings
 *   npx voicesmith-mcp uninstall   Remove everything cleanly
 */

const { BOLD, RESET, DIM } = require("./utils");

const command = process.argv[2];

const USAGE = `
${BOLD}🎙️  VoiceSmith MCP — Local AI Voice System${RESET}

${BOLD}Usage:${RESET}
  npx voicesmith-mcp ${DIM}<command> [options]${RESET}

${BOLD}Commands:${RESET}
  install     Full interactive setup (detects existing installs)
  test        Play a sample voice to verify setup
  voices      Browse and preview all available voices
  config      Change default voice or other settings
  uninstall   Remove everything cleanly

${BOLD}Install Options:${RESET}
  --claude    Configure for Claude Code
  --cursor    Configure for Cursor
  --codex     Configure for Codex (OpenAI)
  --all       Configure for all supported IDEs

${BOLD}Examples:${RESET}
  npx voicesmith-mcp install              ${DIM}# Auto-detect IDEs${RESET}
  npx voicesmith-mcp install --claude     ${DIM}# Claude Code only${RESET}
  npx voicesmith-mcp install --cursor     ${DIM}# Cursor only${RESET}
  npx voicesmith-mcp install --all        ${DIM}# All IDEs${RESET}
  npx voicesmith-mcp test                 ${DIM}# Hear a sample voice${RESET}
  npx voicesmith-mcp uninstall            ${DIM}# Clean removal${RESET}
`;

async function main() {
  switch (command) {
    case "install":
      await require("./install").run();
      break;
    case "uninstall":
      await require("./uninstall").run();
      break;
    case "test":
      await require("./test-voice").run();
      break;
    case "voices":
      await require("./voices").run();
      break;
    case "config":
      await require("./config").run();
      break;
    case "--help":
    case "-h":
    case undefined:
      console.log(USAGE);
      break;
    default:
      console.error(`Unknown command: ${command}`);
      console.log(USAGE);
      process.exit(1);
  }
}

main().catch((err) => {
  console.error(`\nFatal error: ${err.message}`);
  process.exit(1);
});
