import React from "react";
import {Box} from "@anthropic/ink";
import {geometry} from "../theme/tokens.js";
import {SelectionText} from "../components/Selection.js";
import {useCopy} from "./PageFrame.js";
import type {Copy,Selectable} from "./pageContract.js";
export type PageAction<Id extends string|number> = Selectable<Id> & {label:Copy};
export function PageActions<Id extends string|number>({items,selected,focused}:{items:readonly PageAction<Id>[];selected:Id|undefined;focused:boolean}){
 const t=useCopy();
 return <Box flexDirection="row" flexWrap="wrap" gap={geometry.actions.gap} flexShrink={0}>{items.map(item=>{
  const active=focused&&item.enabled&&item.id===selected;
  return <SelectionText key={item.id} selected={active} disabled={!item.enabled} variant="block">{t(item.label)}</SelectionText>;
 })}</Box>;
}
