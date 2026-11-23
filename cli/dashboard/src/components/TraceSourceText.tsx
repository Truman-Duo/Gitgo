import React,{useCallback,useEffect,useMemo,useState} from "react";
import {Box,Text} from "@anthropic/ink";
import {INPUT_SOURCE_PAGE_CHARS,type InputSourcePage,type TraceInputSourceReader} from "../chat/traceSource.js";
import {TraceContent} from "./TraceContent.js";
import {VirtualContent} from "./VirtualContent.js";

const segments=new Intl.Segmenter(undefined,{granularity:"grapheme"});
export function inputPageDisplayText(page:InputSourcePage):string {
 const before=page.before||"",all=before+page.text+(page.after||"");
 const start=before.length,end=start+page.text.length,parts:string[]=[];
 for(const part of segments.segment(all))if(part.index>=start&&part.index<end)parts.push(part.segment);
 return parts.join("");
}
function PageText({page,width,onTotal}:{page:InputSourcePage;width:number;onTotal:(total:number)=>void}) {
 useEffect(()=>onTotal(page.total_chars),[page.total_chars,onTotal]);
 const content=useMemo(()=>inputPageDisplayText(page),[page]);
 return <Box width={Math.max(1,width)}><Text dimColor wrap="wrap">{content.replace(/\r?\n$/,"")}</Text></Box>;
}
function SourcePage({reference,index,offset,read,width,onTotal,loadingRows}:{
 reference:string;index:number;offset:number;read:TraceInputSourceReader;width:number;onTotal:(total:number)=>void;loadingRows:number;
}) {
 const load=useCallback((_key:string,signal:AbortSignal)=>read(reference,index,offset,signal),[reference,index,offset,read]);
 return <TraceContent detailRef={JSON.stringify([reference,index,offset])} readDetail={load} loadingRows={loadingRows}>
  {(value:unknown)=>value ? <PageText page={value as InputSourcePage} width={width} onTotal={onTotal}/> : null}
 </TraceContent>;
}
/** One bounded page path for original model input and saved tool text. */
export function TraceSourceText({reference,index,read,width,totalChars}:{
 reference:string;index:number;read:TraceInputSourceReader;width:number;totalChars?:number;
}) {
 const [total,setTotal]=useState<number|undefined>(totalChars);
 const keys=useMemo(()=>Array.from({length:Math.max(1,Math.ceil((total||0)/INPUT_SOURCE_PAGE_CHARS))},(_,page)=>String(page)),[total]);
 const estimate=useCallback((_key:string,page:number)=>Math.max(1,Math.ceil(Math.min(INPUT_SOURCE_PAGE_CHARS,
  (total??INPUT_SOURCE_PAGE_CHARS)-page*INPUT_SOURCE_PAGE_CHARS)/Math.max(1,width))),[total,width]);
 return <VirtualContent itemKeys={keys} minimumItems={1} layoutKey={width} estimateRows={estimate}>
  {page=><SourcePage reference={reference} index={index} offset={page*INPUT_SOURCE_PAGE_CHARS}
   read={read} width={width} onTotal={setTotal} loadingRows={estimate("",page)}/>}
 </VirtualContent>;
}
