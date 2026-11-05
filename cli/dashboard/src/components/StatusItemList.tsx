import React from "react";
import {Box,Text} from "@anthropic/ink";
import {ConnectivityDot,type ConnectivityState} from "./ConnectivityDot.js";
import {SelectionText} from "./Selection.js";
import {geometry} from "../theme/tokens.js";
/** Read-only collection, independent of navigation. Keep the current item visible. */
export function StatusItemList({title,items,currentId,maxVisible=6}:{title:string;items:{id:string;label:string;state:ConnectivityState}[];currentId?:string;maxVisible?:number}){
 const count=Math.max(1,maxVisible),shown=items.slice(0,count),current=items.find(p=>p.id===currentId);
 if(current&&!shown.some(p=>p.id===currentId))shown[shown.length-1]=current;
 return <Box flexDirection="column" flexShrink={0}>
  <Text dimColor>{title} ({items.length})</Text>
  <Box flexDirection="row" flexWrap="wrap" gap={geometry.actions.gap}>
   {shown.map(p=><Box key={p.id}><ConnectivityDot state={p.state}/><SelectionText selected={p.id===currentId} highlight={false}>{p.label}</SelectionText></Box>)}
   {items.length>shown.length?<Text dimColor>+{items.length-shown.length}</Text>:null}
  </Box>
 </Box>;
}
