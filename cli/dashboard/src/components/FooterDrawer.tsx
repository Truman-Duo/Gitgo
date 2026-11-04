import React from "react";
import {Box} from "@anthropic/ink";
import {InputLayer,ScrollableInputLayer} from "../input/runtime.js";
import {footerDrawerHeight} from "../ui/overlayLayout.js";

/** Bounded footer region. The CommandBar owns its placement between editor and
 * status; callers own business state, and reuse this region's existing input layers. */
export function FooterDrawer({terminalRows,viewport="shared",children}:{terminalRows:number;viewport?:"shared"|"self";children:React.ReactNode}) {
 return <Box maxHeight={footerDrawerHeight(terminalRows)} minHeight={0} flexDirection="column" flexShrink={0}>
  {viewport==="self"?<InputLayer priority={100}>{children}</InputLayer>:<ScrollableInputLayer>{children}</ScrollableInputLayer>}
 </Box>;
}
