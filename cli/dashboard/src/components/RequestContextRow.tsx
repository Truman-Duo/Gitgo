import React,{useMemo} from "react";
import {Box,Text} from "@anthropic/ink";
import type {RequestInputEvent} from "../types.js";
import type {TraceBodyReader} from "../chat/traceSource.js";
import {inputRecord,recordedInputSources} from "../chat/requestInputs.js";
import {geometry} from "../theme/tokens.js";
import {useCopy} from "../ui/PageFrame.js";
import {Disclosure} from "./Disclosure.js";
import {TraceContent} from "./TraceContent.js";
import {VirtualContent} from "./VirtualContent.js";
import {TraceSourceText} from "./TraceSourceText.js";
import {DetailViewport} from "./DetailViewport.js";
import {DisclosureScope} from '../hooks/disclosureScope.js';
import {useDisclosureState} from '../hooks/useDisclosureState.js';
export {inputPageDisplayText} from "./TraceSourceText.js";

const inline=(value:unknown)=>String(value ?? "").replace(/[\x00-\x1f\x7f-\x9f]/g," ");

function InputSource({source,index,reference,read,width}: {
 source:Record<string,any>;index:number;reference:string;read:TraceBodyReader;width:number;
}) {
  const t=useCopy();
  const [expanded,setExpanded]=useDisclosureState(false,'source:'+index);
  const onToggle=()=>setExpanded(previous=>!previous);
  const sections=Array.isArray(source.sections) ? source.sections.map(inputRecord) : [];
  const label=sections.length ? sections.map(section=>inline(section.name || t({en:"Unnamed source",zh:"未命名来源"}))).join(" · ")
    : inline(source.message_type || t({en:"Source not recorded",zh:"来源未记录"}));
  return <Disclosure label={label} expanded={expanded} onToggle={onToggle}
    contentInset={geometry.record.indent}>
    {expanded ? <DetailViewport maxRows={geometry.record.contextMaxRows} width={Math.max(1,width-geometry.record.indent)} onClose={onToggle}>
     {bodyWidth=><>
      {sections.map((section,index)=><Text dimColor key={index}>
        {inline(section.provenance || t({en:"Origin not recorded",zh:"来源未记录"}))}
        {" · "}{t({en:"Version",zh:"版本"})}: {inline(section.version ?? t({en:"Not recorded",zh:"未记录"}))}
      </Text>)}
      {source.content_sha256 ? <Text dimColor>{t({en:"Content digest",zh:"内容摘要"})}: {inline(source.content_sha256)}</Text> : null}
      <TraceSourceText reference={reference} index={index} read={read.inputSource!} width={bodyWidth}/>
     </>}
    </DetailViewport> : null}
  </Disclosure>;
}

function InputDetails({detail,width,reference,read}: {detail:unknown;width:number;reference:string;read:TraceBodyReader}) {
  const t=useCopy(),{exact,sources}=recordedInputSources(detail);
  const keys=useMemo(()=>sources.map(item=>item.id),[detail]);
  if(!exact)return <Text dimColor>{t({en:"Exact input sources were not recorded for this request.",zh:"本次请求未记录确切的注入来源。"})}</Text>;
  return <Box flexDirection="column">
    {!sources.length ? <Text dimColor>{t({en:"No injected sources were recorded.",zh:"没有已记录的注入来源。"})}</Text> : null}
    <VirtualContent itemKeys={keys} layoutKey={width} estimateRows={1}>
      {index=>{const item=sources[index]!;
       return <InputSource key={item.id} source={item.source} index={item.source_index} reference={reference} read={read} width={width}/>;}}
    </VirtualContent>
  </Box>;
}

/** A view of one existing request detail. This component cannot submit input,
 * reopen source files, or recreate a missing request from current settings. */
export function RequestContextRow(props:{request:RequestInputEvent;readBody?:TraceBodyReader;width:number;actor?:string}){
 return <DisclosureScope scope={'context:'+JSON.stringify([props.request.process_id,props.request.detail_ref||[props.request.seq,props.request.time]])}><ContextRow {...props}/></DisclosureScope>;
}
function ContextRow({request,readBody,width,actor}: {
  request:RequestInputEvent;readBody?:TraceBodyReader;width:number;actor?:string;
}) {
  const t=useCopy(),[expanded,setExpanded]=useDisclosureState(false,'header');
  const detailRef=String(request.detail_ref || "");
  const label=(actor ? actor+" · " : "")+t({en:"Injected context",zh:"上下文注入"});
  return <Disclosure label={label} expanded={expanded} onToggle={()=>setExpanded(value=>!value)}
    contentInset={geometry.record.indent}>
    {expanded ? detailRef && readBody?.inputSources && readBody.inputSource
       ? <TraceContent detailRef={detailRef} readDetail={readBody.inputSources}>{(detail:unknown)=>detail
         ? <InputDetails key={detailRef} detail={detail} reference={detailRef} read={readBody} width={Math.max(1,width-geometry.record.indent)}/>
         : null}</TraceContent>
       : <Text dimColor>{t({en:"Saved input is unavailable for this request.",zh:"本次请求没有可读取的已保存输入。"})}</Text> : null}
  </Disclosure>;
}
