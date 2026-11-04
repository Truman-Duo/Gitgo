import {geometry} from "../theme/tokens.js";

/** One presentation classification for the App shell and scoped-help wrapper. */
export function isFooterDrawer(type=""):boolean {
 return ["btwPanel","compactPanel","modelPicker","effortPicker","interruptConfirm","undoPanel"].includes(type);
}

export function footerDrawerHeight(terminalRows:number):number {
 return Math.max(1,Math.floor((terminalRows>0?terminalRows:24)*geometry.drawer.maxHeightRatio));
}
