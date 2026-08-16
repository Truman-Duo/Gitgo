// src/theme/index.ts — Unified theme module exports.

export { colors, toolColor, toolIcon } from "./tokens.js";
export {
  diffAvail,
  diffLineNo,
  diffCell,
  unifiedColWidth,
  splitColWidth,
  DIFF_FRAME_OVERHEAD,
} from "./diffLayout.js";
export { useSelectionStyle } from "./useSelectionStyle.js";
export { useInputStyle } from "./useInputStyle.js";
export { useSuggestionStyle } from "./useSuggestionStyle.js";
export { usePanelSize } from "./usePanelSize.js";
export { useColorTransition } from "./useColorTransition.js";
export { sortByName } from "./useSortAlpha.js";
export {
  truncate,
  wrap,
  lerpColor,
  statusDot,
  placeholderChar,
  separator,
  indent,
  scrollHint,
  badgeBg,
  spinnerFrame,
  contextBarFill,
  contextPct,
  processStatusToDot,
  projectStatusDot,
  partitionByRank,
} from "./typography.js";
export type {
  SemanticColor,
  SelectionContext,
  SelectionVariant,
  InputMode,
  StatusState,
  StyleProps,
  PanelSize,
  StatusDot,
} from "./types.js";
