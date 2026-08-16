// src/theme/tokens.ts — Single source of truth for ALL colors and visual constants.
// No other file should define hex color literals or named color strings.

import chalk from "chalk";

// ── Semantic colors ──────────────────────────────────────────
export const colors = {
  success: "#3fb950",
  successBadge: "#1a3d1a",
  successBg: "#0d1a0d",

  warning: "#d29922",
  warningBadge: "#3d351a",

  danger: "#f85149",
  dangerBadge: "#3d1a1a",
  dangerBg: "#2d1a1a",

  accent: "#58a6ff",
  accentBadge: "#1a2d3d",
  accentBg: "#1a2333",

  // ── Named color → hex mapping (replaces "green"/"red"/"yellow"/"gray"/"cyan" bare strings) ──
  named: {
    green: "#3fb950",
    red: "#f85149",
    yellow: "#d29922",
    gray: "#a8a8a8",
    cyan: "#58a6ff", // InlineContext active tab → unified with accent
  },

  // ── Selection ──────────────────────────────────────────────
  selection: {
    row: {
      bg: "#2a2a2a",
      fg: undefined as string | undefined,
    },
    block: {
      bg: "#ffffff",
      fg: "#000000",
      blue: { bg: "#58a6ff", fg: "#000000" },
      green: { bg: "#3fb950", fg: "#000000" },
      silver: { bg: "#a8a8a8", fg: "#000000" },
    },
    dim: {
      block: { bg: "#a8a8a8", alt: "#4a4a4a" },
    },
  },

  // ── Tab headers ────────────────────────────────────────────
  tab: {
    active: { bg: "#afafaf", fg: "#000000" },
    detail: { bg: "#3f3f3f", fg: "#878787" },
  },

  // ── Text input / command bar ───────────────────────────────
  input: {
    normal: {
      prompt: chalk.cyan.bold("▸ "), // ▸
      border: "#58a6ff",
      badge: chalk.bgHex("#58a6ff").black.bold(" NORMAL "),
    },
    command: {
      prompt: chalk.green.bold("/ "),
      bg: "#0d1a0d",
      border: "#3fb950",
      badge: chalk.bgGreen.black.bold(" COMMAND "),
    },
    result: { fg: "#d29922" },
  },

  // ── Suggestion list ────────────────────────────────────────
  suggestion: {
    active: { fg: "#3fb950", bg: "#1a3d1a" },
  },

  // ── Status dots (3-state + offline) ────────────────────────
  status: {
    error: { char: "●", color: "#f85149", badgeBg: "#3d1a1a" }, // ●
    warning: { char: "◐", color: "#d29922", badgeBg: "#3d351a" }, // ◐
    ok: { char: "●", color: "#3fb950", badgeBg: "#1a3d1a" }, // ●
    offline: { char: "○", color: undefined as string | undefined, badgeBg: undefined as string | undefined }, // ○
    done: { char: "●", color: "#8b949e", badgeBg: "#1a2333" }, // ●
  },

  // ── Role badges (ChatPanel + AgentDetail) ──────────────────
  badge: {
    user: { fg: "#3fb950", bg: "#1a3d1a" },
    agent: { fg: "#58a6ff", bg: "#1a2d3d" },
    system: { fg: undefined as string | undefined, bg: "#1a2333" },
  },

  // ── Chat message layering (user block / gutter) ────────────
  chat: {
    userBg: "#262626",   // 用户提示词灰底块
    gutter: "#6e7681",   // assistant/tool 行首 gutter 标记
    userMarker: "#a8a8a8", // 用户消息行首指针 ❯ 标记
  },

  // ── Edit forms ─────────────────────────────────────────────
  edit: {
    field: { activeBg: "#a8a8a8", activeFg: "#000000" },
    placeholder: { char: "█", color: "#a8a8a8" }, // █ — form placeholder, NOT native cursor
  },

  // ── Confirm buttons ────────────────────────────────────────
  confirm: {
    yes: { bg: "#3fb950", fg: "#000000" },
    no: { bg: "#d29922", fg: "#000000" },
  },

  // ── Dividers ───────────────────────────────────────────────
  divider: {
    char: "─", // ─
    color: "#a8a8a8",
  },

  // ── Context bar (AgentDetail) ──────────────────────────────
  contextBar: {
    low: "#3fb950",
    mid: "#d29922",
    high: "#f85149",
    fill: "█", // █
    empty: "░", // ░
  },

  // ── Spinner ────────────────────────────────────────────────
  spinner: {
    frames: ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
    // ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏
    intervalMs: 80,
    color: "#d29922",
    // Triangle fill pulse (hollow ▹ → large ▷ → solid ▶ → settled ▸) used as the running-tool buffer.
    triangleFrames: ["▹", "▷", "▶", "▸"], // ▹▷▶▸
    triangleIntervalMs: 150,
  },

  // ── Animation ──────────────────────────────────────────────
  animation: {
    lerp: { frames: 4, intervalMs: 37 },
  },

  // ── Tool colors (from toolStyles.ts) ──────────────────────
  tool: {
    read: "#58a6ff",
    write: "#3fb950",
    exec: "#d29922",
  },

  // ── Diff (side-by-side / unified) ─────────────────────────
  diff: {
    added: "#3fb950",
    removed: "#f85149",
    addedBg: "#12261e",
    removedBg: "#2d1a1a",
    lineNumber: "#6e7681",
    frame: "#6e7681",
  },
} as const;

// ── Tool color helpers (from toolStyles.ts) ───────────────────
const TOOL_COLOR_MAP: Record<string, string> = {
  read_file: colors.tool.read,
  scan: colors.tool.read,
  glob: colors.tool.read,
  grep: colors.tool.read,
  write_file: colors.tool.write,
  edit: colors.tool.write,
  bash: colors.tool.exec,
  shell: colors.tool.exec,
  execute: colors.tool.exec,
  dispatch_tool: colors.tool.exec,
  fork_agent: colors.tool.exec,
};

export function toolColor(name: string): string {
  for (const [key, color] of Object.entries(TOOL_COLOR_MAP)) {
    if (name.includes(key) || key.includes(name)) return color;
  }
  return "";
}

export function toolIcon(_name: string): string {
  return "▸"; // ▸ — unified geometry glyph; color still varies via toolColor
}
