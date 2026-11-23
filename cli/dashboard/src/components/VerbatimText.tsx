import React,{useMemo} from "react";
import {Text,Box} from "@anthropic/ink";
import {VirtualContent} from "./VirtualContent.js";

/** Saved file/tool text is not Markdown. The existing viewport mounts rows. */
export function VerbatimText({content,width}:{content:string;width:number}) {
 const lines=useMemo(()=>content.split("\n"),[content]);
 const keys=useMemo(()=>lines.map((_,index)=>String(index)),[lines]);
 return <VirtualContent itemKeys={keys} minimumItems={30} layoutKey={width}
  estimateRows={(_key,index)=>Math.max(1,Math.ceil(lines[index]!.length/Math.max(1,width)))}>
  {index=><Box minHeight={1} flexShrink={0}><Text dimColor wrap="wrap">{lines[index]||" "}</Text></Box>}
 </VirtualContent>;
}
