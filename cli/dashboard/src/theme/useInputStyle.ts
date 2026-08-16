// src/theme/useInputStyle.ts — Text input styling hook (NORMAL/COMMAND modes).

import type { InputMode } from "./types.js";
import { colors } from "./tokens.js";

export function useInputStyle(mode: InputMode): {
  promptChalk: string;
  bg: string | undefined;
  borderColor: string;
  badge: string;
  resultFg: string;
} {
  if (mode === "COMMAND") {
    return {
      promptChalk: colors.input.command.prompt,
      bg: colors.input.command.bg,
      borderColor: colors.input.command.border,
      badge: colors.input.command.badge,
      resultFg: colors.input.result.fg,
    };
  }

  return {
    promptChalk: colors.input.normal.prompt,
    bg: undefined,
    borderColor: colors.input.normal.border,
    badge: colors.input.normal.badge,
    resultFg: colors.input.result.fg,
  };
}
