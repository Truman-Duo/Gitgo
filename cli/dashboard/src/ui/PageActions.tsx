import React from "react";
import {Box,Text} from "@anthropic/ink";
import {colors} from "../theme/index.js";
import {useCopy} from "./PageFrame.js";
import type {Copy,Selectable} from "./pageContract.js";
export type PageAction<Id extends string|number> = Selectable<Id> & {label:Copy};
export function PageActions<Id extends string|number>({items,selected,focused}:{items:readonly PageAction<Id>[];selected:Id|undefined;focused:boolean}){
 const t=useCopy();
 return <Box flexDirection="row" flexWrap="wrap" gap={2} flexShrink={0}>{items.map(item=>{
  const active=focused&&item.enabled&&item.id===selected;
  return <Text key={item.id} dimColor={!item.enabled} color={active?colors.selection.block.fg:undefined}
   backgroundColor={active?colors.selection.block.bg:undefined}>{t(item.label)}</Text>;
 })}</Box>;
}
