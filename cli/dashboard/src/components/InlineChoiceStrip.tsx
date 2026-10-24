import React from "react";
import {Box,Text} from "@anthropic/ink";
import {colors,truncate} from "../theme/index.js";
import {fitHeaderWindow,type HeaderStripItem} from "./HorizontalHeaderStrip.js";

type Props={items:HeaderStripItem[];selected:number;width:number;interactive?:boolean};
/** Compact choices inside a page; navigation headers keep their own spacing. */
export function InlineChoiceStrip({items,selected,width,interactive=true}:Props){
 const budget=Math.max(1,width);
 const cap=Math.max(1,Math.min(24,budget-2));
 const labels=items.map(item=>({...item,label:truncate(item.label,cap)}));
 // Reserve one inter-choice cell as well as the selected label's padding.
 const window=fitHeaderWindow(labels.map(item=>({...item,label:item.label+" "})),selected,budget);
 const selection=interactive?colors.selection.block:colors.selection.dim.block;
 return <Box flexDirection="row" width={budget} height={1} minHeight={1} flexShrink={0} gap={1} overflow="hidden">
  {window.left?<Text dimColor>...</Text>:null}
  {labels.slice(window.start,window.end).map((item,offset)=><Text key={item.id}
   color={window.start+offset===selected?colors.selection.block.fg:colors.tab.detail.fg}
   backgroundColor={window.start+offset===selected?selection.bg:undefined}
   bold={interactive&&window.start+offset===selected} dimColor={window.start+offset!==selected}>
   {" "+truncate(item.label,Math.max(1,window.slotWidth-3))+" "}
  </Text>)}
  {window.right?<Text dimColor>...</Text>:null}
 </Box>;
}
