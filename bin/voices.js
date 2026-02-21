/**
 * Agent Voice MCP — Browse available voices.
 *
 * Displays all Kokoro voices grouped by language and gender.
 */

const {
  logHeader,
  BOLD,
  RESET,
  DIM,
  GREEN,
} = require("./utils");

// Voice catalog (matches shared.py VOICE_METADATA)
const VOICES = {
  "American English": {
    female: [
      "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica",
      "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    ],
    male: [
      "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
      "am_michael", "am_onyx", "am_puck", "am_santa",
    ],
  },
  "British English": {
    female: ["bf_alice", "bf_emma", "bf_isabella", "bf_lily"],
    male: ["bm_daniel", "bm_fable", "bm_george", "bm_lewis"],
  },
  Spanish: {
    female: ["ef_dora"],
    male: ["em_alex", "em_santa"],
  },
  French: {
    female: ["ff_siwis"],
  },
  Hindi: {
    female: ["hf_alpha", "hf_beta"],
    male: ["hm_omega", "hm_psi"],
  },
  Italian: {
    female: ["if_sara"],
    male: ["im_nicola"],
  },
  Japanese: {
    female: ["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro"],
    male: ["jm_kumo"],
  },
  Portuguese: {
    female: ["pf_dora"],
    male: ["pm_alex", "pm_santa"],
  },
  Mandarin: {
    female: ["zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi"],
    male: ["zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang"],
  },
};

async function run() {
  logHeader();
  console.log(`${BOLD}Available Kokoro Voices${RESET}\n`);

  let total = 0;

  for (const [lang, genders] of Object.entries(VOICES)) {
    console.log(`  ${BOLD}${lang}${RESET}`);
    for (const [gender, voices] of Object.entries(genders)) {
      const label = gender === "female" ? "♀" : "♂";
      const names = voices
        .map((v) => {
          const name = v.split("_").slice(1).join("_");
          return `${DIM}${v.split("_")[0]}_${RESET}${name}`;
        })
        .join("  ");
      console.log(`    ${label}  ${names}`);
      total += voices.length;
    }
    console.log("");
  }

  console.log(`  ${GREEN}Total: ${total} voices${RESET}\n`);
  console.log(
    `  ${DIM}Use 'set_voice' MCP tool to assign any voice to an agent name.${RESET}\n`
  );
}

module.exports = { run };
