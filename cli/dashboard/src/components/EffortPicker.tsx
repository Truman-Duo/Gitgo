import React,{useEffect,useRef,useState} from "react";
import {Box,Text} from "@anthropic/ink";
import {DiscreteScale} from "./DiscreteScale.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord,chordLabel} from "../input/bindings.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {llmEffortSelect, type EffortOptions} from "../backend/tools.js";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";

type Props={client:BackendClient;options:EffortOptions;onFooter:(footer:FooterConfig|null)=>void;onDismiss:()=>void;onSelected?:(result:any)=>void};
export function EffortPicker(props:Props){return <ModuleHelpBoundary inline onFooter={props.onFooter}>{onFooter=><EffortPickerBody {...props} onFooter={onFooter}/>}</ModuleHelpBoundary>;}
function EffortPickerBody({client,options,onFooter,onDismiss,onSelected}:Props){
 const t=useCopy(),{w}=usePanelSize({widthOffset:6});
 const ids=Array.from(new Set([...options.levels,...(options.effort?[options.effort]:[])]));
 const [value,setValue]=useState(()=>[options.effort,options.effective_effort,options.default_effort,"medium","high"]
  .find((id): id is string => !!id && ids.includes(id)) ?? ids[0] ?? "");
 const [busy,setBusy]=useState(false);
 const alive=useRef(true),saving=useRef(false),feedback=useFeedback({kind:"page",id:"effort-picker"});
 useEffect(()=>{onFooter(null);return()=>onFooter(null);},[onFooter]);
 const help=useLocalHelpCommand("effort",onFooter,!busy);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 const submit=async()=>{if(!ids.includes(value)||saving.current)return;if(value===options.effort){onDismiss();return;}
  saving.current=true;setBusy(true);
  try{const r:any=await llmEffortSelect(client,options.provider_id,options.model_id,value,options.revision);if(r.error)throw r.error;if(alive.current){onSelected?.(r);onDismiss();}}
  catch(e:any){if(alive.current)feedback.show(`${t({en:"Effort switch failed; previous value retained",zh:"思考强度切换失败，保留原值"})} · ${e?.code || e?.message || "EFFORT_SWITCH_FAILED"}`,{tone:"error"});}
  finally{saving.current=false;if(alive.current)setBusy(false);}
 };
 useManagedInput((input,key)=>{if(help.handle(input,key))return true;if(matchChord("escape",input,key)){if(!saving.current)onDismiss();return true;}if(busy)return true;
  if(matchChord("left",input,key)||matchChord("right",input,key)){const index=ids.indexOf(value),right=matchChord("right",input,key);setValue(ids[index<0?(right?0:ids.length-1):Math.max(0,Math.min(ids.length-1,index+(right?1:-1)))] || "");}
  else if(matchChord("enter",input,key))void submit();return true;
 },{clipboard:false});
 return <Box flexDirection="column" minHeight={0}>
  {busy?<Text dimColor>{t({en:"Saving…",zh:"正在保存…"})}</Text>:null}
  <DiscreteScale width={w} value={value} items={ids.map(id=>({id,label:id}))} startLabel={t({en:"Faster",zh:"更快"})} endLabel={t({en:"Deeper",zh:"更深入"})}/>
  <FeedbackMessage feedback={feedback.message}/>
 </Box>;
}
