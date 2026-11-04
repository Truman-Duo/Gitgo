import React from "react";
import {Box,Text,stringWidth} from "@anthropic/ink";
import {colors,truncate} from "../theme/index.js";
import {geometry} from "../theme/tokens.js";

/** Controlled, bounded horizontal scale; the scene owns keys and persistence. */
export function DiscreteScale({items,value,width,startLabel,endLabel}:{items:{id:string;label:string}[];value:string;width:number;startLabel?:string;endLabel?:string}){
 const selected=Math.max(0,items.findIndex(item=>item.id===value));
 const cell=Math.max(1,Math.max(0,...items.map(p=>stringWidth(p.label)))+2);
 const available=Math.max(1,Math.min(Math.floor(width),Math.max(geometry.scale.preferredWidth,cell*Math.max(1,items.length))));
 const count=Math.max(1,Math.min(items.length,Math.floor(available/cell)));
 const start=Math.max(0,Math.min(selected-Math.floor(count/2),items.length-count)),shown=items.slice(start,start+count);
 const anchors=shown.map((_,i)=>shown.length<2?0:Math.round(i*(available-1)/(shown.length-1)));
 const labels=shown.map(p=>truncate(p.label,available));
 const labelStarts=labels.map((label,i)=>i===0?0:i===labels.length-1?available-stringWidth(label):Math.max(0,Math.min(available-stringWidth(label),anchors[i]!-Math.floor(stringWidth(label)/2))));
 const endpointWidth=Math.floor(available/2);
 return <Box flexDirection="column" width={available}>
  <Box><Box width={endpointWidth}><Text dimColor wrap="truncate-end">{startLabel}</Text></Box><Box width={available-endpointWidth} justifyContent="flex-end"><Text dimColor wrap="truncate-end">{endLabel}</Text></Box></Box>
  <Box>{shown.map((p,index)=><React.Fragment key={p.id}><Text color={colors.divider.color}>{"─".repeat(Math.max(0,anchors[index]!-(index?anchors[index-1]!+1:0)))}</Text><Text color={p.id===value?colors.scale.active:colors.divider.color}>{p.id===value?"▲":"─"}</Text></React.Fragment>)}<Text color={colors.divider.color}>{"─".repeat(Math.max(0,available-1-(anchors.at(-1)??-1)))}</Text></Box>
  <Box>{labels.map((label,index)=><React.Fragment key={shown[index]!.id}><Text>{" ".repeat(Math.max(0,labelStarts[index]!-(index?labelStarts[index-1]!+stringWidth(labels[index-1]!):0)))}</Text><Text dimColor wrap="truncate-end">{label}</Text></React.Fragment>)}</Box>
 </Box>;
}
