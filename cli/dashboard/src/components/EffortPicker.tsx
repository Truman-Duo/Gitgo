import React,{useEffect,useRef,useState} from "react";
import {Box,Text} from "@anthropic/ink";
import {DiscreteScale} from "./DiscreteScale.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord,chordLabel} from "../input/bindings.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {llmEffortOptions,llmEffortSelect} from "../backend/tools.js";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";

type Props={client:BackendClient;onFooter:(footer:FooterConfig|null)=>void;onDismiss:()=>void;onSelected?:(result:any)=>void};
export function EffortPicker(props:Props){return <ModuleHelpBoundary inline onFooter={props.onFooter}>{onFooter=><EffortPickerBody {...props} onFooter={onFooter}/>}</ModuleHelpBoundary>;}
function EffortPickerBody({client,onFooter,onDismiss,onSelected}:Props){
 const t=useCopy(),{w}=usePanelSize({widthOffset:6});
 const [data,setData]=useState<any>(null),[value,setValue]=useState(""),[busy,setBusy]=useState(true);
 const alive=useRef(true),saving=useRef(false),feedback=useFeedback({kind:"page",id:"effort-picker"});
 useEffect(()=>{onFooter(null);return()=>onFooter(null);},[onFooter]);
 const help=useLocalHelpCommand("effort",onFooter,!saving.current);
 const effective=(r:any)=>r?.effort || r?.effective_effort || r?.default_effort || "";
 const initial=(r:any)=>effective(r)||["high","medium"].find(id=>r?.levels?.includes(id))||r?.levels?.[0]||"";
 useEffect(()=>{alive.current=true;void llmEffortOptions(client).then((r:any)=>{if(r.error)throw r.error;if(alive.current){setData(r);setValue(initial(r));}}).catch((e:any)=>{if(alive.current)feedback.show(`${t({en:"Unable to read effort settings",zh:"无法读取思考强度配置"})} · ${e?.code || e?.message || "EFFORT_READ_FAILED"}`,{tone:"error"});}).finally(()=>{if(alive.current)setBusy(false);});return()=>{alive.current=false;};},[client]);
 const ids=Array.from(new Set([...(data?.levels || []),...(data?.effort?[data.effort]:[])].filter(Boolean))) as string[];
 const submit=async()=>{if(!data?.provider_id||!ids.includes(value)||busy||saving.current)return;if(value===data.effort){onDismiss();return;}
  saving.current=true;setBusy(true);
  try{const r:any=await llmEffortSelect(client,data.provider_id,data.model_id,value,data.revision);if(r.error)throw r.error;if(alive.current){onSelected?.(r);onDismiss();}}
  catch(e:any){if(alive.current)feedback.show(`${t({en:"Effort switch failed; previous value retained",zh:"思考强度切换失败，保留原值"})} · ${e?.code || e?.message || "EFFORT_SWITCH_FAILED"}`,{tone:"error"});}
  finally{saving.current=false;if(alive.current)setBusy(false);}
 };
 useManagedInput((input,key)=>{if(help.handle(input,key))return true;if(matchChord("escape",input,key)){if(!saving.current)onDismiss();return true;}if(busy)return true;
  if(matchChord("left",input,key)||matchChord("right",input,key)){const index=ids.indexOf(value),right=matchChord("right",input,key);setValue(ids[index<0?(right?0:ids.length-1):Math.max(0,Math.min(ids.length-1,index+(right?1:-1)))] || "");}
  else if(matchChord("enter",input,key))void submit();return true;
 },{clipboard:false});
 return <Box flexDirection="column" minHeight={0}>
  {busy?<Text dimColor>{t({en:"Loading / saving…",zh:"正在读取／保存…"})}</Text>:null}
  {ids.length?<DiscreteScale width={w} value={value} items={ids.map(id=>({id,label:id}))} startLabel={t({en:"Faster",zh:"更快"})} endLabel={t({en:"Deeper",zh:"更深入"})}/>:null}
  {data?.state==="unknown"?<Text dimColor>{t({en:"No effort levels declared",zh:"未声明可选档位"})}</Text>:null}
  {data?.state==="unsupported"?<Text dimColor>{t({en:"This model does not declare adjustable effort",zh:"此模型不支持已声明的强度调节"})}</Text>:null}
  {data?.state==="unconfigured"?<Text dimColor>{t({en:"Configure an API and models first",zh:"请先配置 API 和模型"})}</Text>:null}
  <FeedbackMessage feedback={feedback.message}/>
 </Box>;
}
