import React from "react";
import {Box,Text} from "@anthropic/ink";
import {colors,separator} from "../theme/index.js";
import {HorizontalHeaderStrip,type HeaderStripItem} from "../components/HorizontalHeaderStrip.js";
/** Header and its divider are one element; pages cannot forget the second half. */
export function PageHeader(props:{items:HeaderStripItem[];selected:number;width:number;contentFocused:boolean}){
 return <Box flexDirection="column" height={2} minHeight={2} flexShrink={0}>
  <HorizontalHeaderStrip {...props}/><Box height={1} minHeight={1} flexShrink={0}><Text color={colors.divider.color}>{separator(props.width)}</Text></Box>
 </Box>;
}
