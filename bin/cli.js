#!/usr/bin/env node

/**
 * Agent Voice MCP — CLI entry point.
 *
 * Usage:
 *   npx agent-voice-mcp install     Full interactive setup
 *   npx agent-voice-mcp test        Play a sample voice to verify
 *   npx agent-voice-mcp voices      Browse and preview all voices
 *   npx agent-voice-mcp config      Re-run voice picker / change settings
 *   npx agent-voice-mcp uninstall   Remove everything cleanly
 */

const { BOLD, RESET, DIM } = require("./utils");

const command = process.argv[2];

const USAGE = `
${BOLD}🎙️  Agent Voice MCP — Local AI Voice System${RESET}

${BOLD}Usage:${RESET}
  npx agent-voice-mcp ${DIM}<command> [options]${RESET}

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
  npx agent-voice-mcp install              ${DIM}# Auto-detect IDEs${RESET}
  npx agent-voice-mcp install --claude     ${DIM}# Claude Code only${RESET}
  npx agent-voice-mcp install --cursor     ${DIM}# Cursor only${RESET}
  npx agent-voice-mcp install --all        ${DIM}# All IDEs${RESET}
  npx agent-voice-mcp test                 ${DIM}# Hear a sample voice${RESET}
  npx agent-voice-mcp uninstall            ${DIM}# Clean removal${RESET}
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
