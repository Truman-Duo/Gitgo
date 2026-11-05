import React,{useEffect,useRef,useState} from "react";
import {Box} from "@anthropic/ink";
import {SearchableChoiceScene} from "./SearchableChoiceScene.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {useCopy} from "../ui/PageFrame.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {llmModelOptions,llmModelSelect} from "../backend/tools.js";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";

type Props={client:BackendClient;onFooter:(footer:FooterConfig|null)=>void;onDismiss:()=>void;onSelected?:()=>void};
export function ModelPicker(props:Props){return <ModuleHelpBoundary inline onFooter={props.onFooter}>{onFooter=><ModelPickerBody {...props} onFooter={onFooter}/>}</ModuleHelpBoundary>;}
function ModelPickerBody({client,onFooter,onDismiss,onSelected}:Props){
  const t=useCopy(),input=useTextInput("");
  const [data,setData]=useState<any>(null),[loading,setLoading]=useState(true);
  const mounted=useRef(true),saving=useRef(false);
  const feedback=useFeedback({kind:"page",id:"model-picker"});
  useEffect(()=>{mounted.current=true;void llmModelOptions(client).then((r:any)=>{if(r.error)throw r.error;if(mounted.current)setData(r);}).catch((e:any)=>{if(mounted.current)feedback.show(`${t({en:"Unable to read current API models",zh:"无法读取当前 API 的模型"})} · ${e?.code || "MODEL_LIST_FAILED"}`,{tone:"error"});}).finally(()=>{if(mounted.current)setLoading(false);});return ()=>{mounted.current=false;};},[client]);
  const select=async(model:string)=>{
    if(saving.current||!data)return;
    if(model===data.model_id){onDismiss();return;}
    saving.current=true;setLoading(true);
    try{const r:any=await llmModelSelect(client,data.provider_id,model,data.revision);if(r.error)throw r.error;if(mounted.current){onSelected?.();onDismiss();}}
    catch(e:any){if(mounted.current)feedback.show(`${t({en:"Model switch failed; selection unchanged",zh:"模型切换失败，原选择保持不变"})} · ${e?.code || "MODEL_SWITCH_FAILED"}`,{tone:"error"});}
    finally{saving.current=false;if(mounted.current)setLoading(false);}
  };
  return <Box flexDirection="column" minHeight={0}>
    <SearchableChoiceScene presentation="inline" helpTopic="model" showCount={false} title={t({en:"Switch model",zh:"切换模型"})} description={data?.provider_name || t({en:"No current API configured",zh:"尚未配置当前 API"})} currentId={data?.model_id} input={input} onFooter={onFooter} busy={loading}
      items={(data?.models || []).map((p:any)=>({id:p.model_id,title:p.model_id,disabled:loading}))} onSelect={model=>void select(model)} onCancel={()=>{if(!saving.current)onDismiss();}}
      notice={data&&!data.models?.length?{tone:"info",text:t({en:"Configure models for the current API in /config providers",zh:"请先在 /config providers 中配置当前 API 的模型"})}:undefined}/>
    <FeedbackMessage feedback={feedback.message}/>
  </Box>;
}
