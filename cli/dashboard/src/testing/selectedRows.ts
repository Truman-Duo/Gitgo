import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {colors} from "../theme/index.js";

/** Inspect rendered selection backgrounds, rather than a page-specific text prefix. */
export function selectedRows(ink: any): string[] {
  const screen = ink.frontFrame.screen;
  const rgb = colors.selection.row.bg.replace("#", "").match(/../g)!.map(part => parseInt(part, 16));
  const code = `\x1b[48;2;${rgb.join(";")}m`;
  return Array.from({length: screen.height}, (_, y) => {
    const cells = Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)!);
    return cells.some(cell => ink.stylePool.get(cell.styleId).some((style: any) => style.code === code))
      ? cells.map(cell => cell.char).join("") : "";
  }).filter(Boolean);
}
