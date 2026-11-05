import {useTerminalGraphics} from "@anthropic/ink";
export type {GraphicsCapability} from "@anthropic/ink";
let enabled = false;
export function configureTerminalGraphics(altScreen: boolean) {enabled = altScreen && process.env.GITGO_NO_COLOR !== "1";}
export function useGraphicsCapability() {return useTerminalGraphics(enabled);}
