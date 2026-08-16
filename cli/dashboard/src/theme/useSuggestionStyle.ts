// src/theme/useSuggestionStyle.ts — Suggestion list item style hook.

import type { StyleProps } from "./types.js";
import { colors } from "./tokens.js";

export function useSuggestionStyle(active: boolean): StyleProps & { dimColor: boolean } {
  if (active) {
    return {
      fg: colors.suggestion.active.fg,
      bg: colors.suggestion.active.bg,
      bold: true,
      dimColor: false,
    };
  }
  return { fg: undefined, bg: undefined, bold: false, dimColor: true };
}
