import React,{useEffect,useRef,useState} from "react";
import {randomUUID} from "node:crypto";
import {Box,Text} from "@anthropic/ink";
import {useCopy} from "../../ui/PageFrame.js";
import {useManagedInput} from "../../input/runtime.js";
import {matchChord,chordLabel} from "../../input/bindings.js";
import {applyCommandAction,resolveCommandKeys,isCommandMode} from "../../input/commandInput.js";
import {resolveDialogSelectKey} from "../../input/overlays/dialogSelect.js";
import {cycleChoiceIndex} from "../../input/choice.js";
import {applyTextOp} from "../../hooks/useTextInput.js";
import {useStagedForm} from "../../hooks/useStagedForm.js";
import {useFeedback} from "../../hooks/useFeedback.js";
import {usePanelSize} from "../../theme/index.js";
import {FormFieldRow,fieldLabelWidth} from "../FormFieldRow.js";
import {SelectionText} from "../Selection.js";
import {NavigableListRow} from "../NavigableListRow.js";
import {SearchableChoiceScene} from "../SearchableChoiceScene.js";
import {DocumentEditorScene} from "../DocumentEditorScene.js";
import {HelpPanel} from "../HelpPanel.js";
import {useModuleHelp} from "../ModuleHelpBoundary.js";
import {FeedbackMessage} from "../FeedbackMessage.js";
import {InlineNotice} from "../InlineNotice.js";
import {providerModelIssue} from "../../ui/providerConfiguration.js";
import {ConnectivityDot,type ConnectivityState} from "../ConnectivityDot.js";
import {llmCancel,llmModels,llmPresets,llmConnection,llmProbe,llmSave} from "../../backend/tools.js";
import type {LLMProvider} from "../../hooks/useLLMConfig.js";
import type {ConfigTabProps} from "./types.js";
import {loadedProfiles,defaultProfile,configurationJson,parseConfiguration,manualModelIds,protocols,type Profile,type Model,type Connection} from "./providerEditorDraft.js";

type Preset={id:string;name:string;base_url:string;protocol:string};
const custom:Preset={id:"custom",name:"Custom",base_url:"",protocol:"openai_chat"};
const baseFields=["preset","name","base_url","api_key","protocol"];

/** One API draft, one model collection and one configuration commit. */
export function ProviderEditor({client,project,cmdInput,onFooter,seed,local:requestedLocal=false,initialTest=false,initialModels=false,onDone}:Pick<ConfigTabProps,"client"|"project"|"cmdInput"|"onFooter"> & {seed?:LLMProvider;local?:boolean;initialTest?:boolean;initialModels?:boolean;onDone:(saved:boolean,message?:string)=>void}){
 const [persisted,setPersisted]=useState(seed);
 const moduleHelp=useModuleHelp();
 const returnField=useRef<string|null>(null);
 const [manualCandidates,setManualCandidates]=useState<Profile[]>([]);
 const t=useCopy(),{w}=usePanelSize({widthOffset:4}),local=requestedLocal||seed?.kind==="local";
 const [profiles,setProfiles]=useState<Profile[]>(()=>loadedProfiles(seed)),profilesRef=useRef(profiles);profilesRef.current=profiles;
 const fields=[...baseFields,...profiles.map(p=>"model:"+p.model_id),"add","advanced"];
 const form=useStagedForm<Connection>({preset:String(seed?.configuration?.preset_id || "custom"),name:seed?.name || "",base_url:seed?.base_url || (local?"http://127.0.0.1:11434/v1":""),api_key:"",protocol:seed?.protocol || "openai_chat"},fields,["protocol"],cmdInput);
 const draft=form.current,current=useRef(draft);current.current=draft;
 const [presets,setPresets]=useState<Preset[]>([custom]),[catalog,setCatalog]=useState<Profile[]>([]);
 const [scene,setScene]=useState<"presets"|"add"|"json"|"help"|null>(!seed&&!local?"presets":null);
 const [presetOrigin,setPresetOrigin]=useState<"entry"|"field">("entry"),presetChosen=useRef(Boolean(seed)||local);
 const [chosen,setChosen]=useState<string[]>([]),[jsonSeed,setJsonSeed]=useState("");
 const [busy,setBusy]=useState<"models"|"connect"|"probe"|"">(""),[saving,setSaving]=useState(false),savingRef=useRef(false);
 const [snapshot,setSnapshot]=useState<{reference:boolean;partial:boolean;credentialsRequired:boolean;ids:string[]}|null>(null);
 const [modelIssue,setModelIssue]=useState<{connection:string;copy:ReturnType<typeof providerModelIssue>}|null>(null);
 const [health,setHealth]=useState<Record<string,{state:ConnectivityState;reason?:string}>>({});
 const feedback=useFeedback({kind:"page",id:`provider-editor:${project}`});
 const [suggestionIdx,setSuggestionIdx]=useState(0);
 const suggestions=!form.editing&&cmdInput.value.startsWith("/")&&"/help".startsWith(cmdInput.value)?[{label:t({en:"/help",zh:"/help"}),description:t({en:"Provider configuration help",zh:"Provider 配置帮助"}),inputMode:"execute" as const}]:[];
 const alive=useRef(true),pending=useRef(""),pendingHealth=useRef(""),generation=useRef(0),creationId=useRef(randomUUID().replaceAll("-","").slice(0,24));
 const discoveryAttempt=useRef(""),connectionAttempts=useRef(new Set<string>()),pendingAction=useRef("");
 const connection=(d:Connection)=>JSON.stringify([d.base_url,d.api_key,d.protocol,local]);
 const identity=(d:Connection,p:Profile)=>JSON.stringify([connection(d),p.model_id,p.protocol,p.request_preferences]);
 const retained=(d:Connection)=>Boolean(persisted&&!d.api_key&&d.base_url===persisted.base_url);
 const ready=(d:Connection)=>Boolean(d.base_url.trim()&&(local||d.api_key.trim()||retained(d)));
 const payload=(d:Connection)=>({provider_id:persisted?.id || "",base_url:d.base_url,api_key:d.api_key,protocol:d.protocol,kind:(local?"local":"remote") as "local"|"remote",auth_mode:(local?(seed?.auth_mode || "none"):"api_key") as "none"|"api_key",retain_api_key:retained(d)});
 const error=(e:any)=>feedback.show(`${t({en:"Operation failed; draft retained",zh:"操作失败，已保留草稿"})} · ${e?.code || e?.message || "PROVIDER_REQUEST_FAILED"}`,{tone:"error"});
 const cancel=(retryDiscovery=false)=>{++generation.current;if(pending.current){if(retryDiscovery&&pendingAction.current==="models")discoveryAttempt.current="";void llmCancel(client,pending.current).catch(()=>{});}pending.current="";pendingAction.current="";setBusy("");if(pendingHealth.current){const key=pendingHealth.current;pendingHealth.current="";setHealth(state=>({...state,[key]:{state:"unknown"}}));}};
 useEffect(()=>{alive.current=true;let valid=true;void llmPresets(client).then((r:any)=>{if(valid&&Array.isArray(r.presets))setPresets(r.presets);}).catch(()=>{});return()=>{valid=false;alive.current=false;++generation.current;if(pending.current)void llmCancel(client,pending.current).catch(()=>{});};},[client]);
 const request=async(action:"models"|"connect"|"probe",profile?:Profile)=>{
  // Requests use staged connection fields, never partially typed credentials.
  const d={...form.values};if(!d.base_url||action!=="models"&&(!ready(d)||!profile))return;
  cancel();const token=++generation.current,id=`provider-${randomUUID()}`;pending.current=id;pendingAction.current=action;setBusy(action);
  if(action==="models"){discoveryAttempt.current=connection(d);setModelIssue(null);}
  const key=profile?identity(d,profile):"";
  if(action!=="models"){pendingHealth.current=key;setHealth(state=>({...state,[key]:{state:"checking"}}));}
  try{
   const result:any=await(action==="models"?llmModels:action==="probe"?llmProbe:llmConnection)(client,{...payload(d),...(profile?{model_id:profile.model_id,protocol:profile.protocol,request_preferences:profile.request_preferences}:{})},id);
   if(result.error)throw result.error;
   if(!alive.current||token!==generation.current||connection(current.current)!==connection(d))return;
   if(action==="models"){
    const models:Model[]=result.models || [],index=new Map(models.map(m=>[m.id,m]));
    setSnapshot({reference:Boolean(result.reference_only),partial:Boolean(result.partial),credentialsRequired:Boolean(result.credentials_required),ids:models.map(m=>m.id)});
    setCatalog(models.map(m=>defaultProfile(m.id,d.protocol,m,result.receipt_id)));
    setProfiles(items=>items.map(p=>{const m=index.get(p.model_id);return m?{...p,deployment:m,model_receipt:p.protocol===d.protocol?result.receipt_id:undefined,context_source:["discovered","reference"].includes(p.context_source)&&p.context_window!==m.limits.context_window?"user":p.context_source}:p;}));
   }else{
    setHealth(state=>({...state,[key]:{state:result.checks?.model_response==="verified"?"online":"unknown"}}));
    if(action==="probe"&&profile){connectionAttempts.current.add(identity(d,{...profile,protocol:result.protocol}));setProfiles(items=>items.map(p=>p.model_id===profile.model_id?{...p,protocol:result.protocol,probe_receipt:result.receipt_id}:p));}
   }
  }catch(e:any){if(alive.current&&token===generation.current&&connection(current.current)===connection(d)){if(action==="models")setModelIssue({connection:connection(d),copy:providerModelIssue(e)});else setHealth(state=>({...state,[key]:{state:"offline",reason:e?.code || e?.message || "PROVIDER_CONNECTION_FAILED"}}));}}
  finally{if(alive.current&&token===generation.current){pending.current="";pendingAction.current="";pendingHealth.current="";setBusy("");}}
 };
 useEffect(()=>{if(scene==="presets"||form.editing||busy||saving||!draft.base_url)return;const key=connection(draft);if(discoveryAttempt.current===key)return;discoveryAttempt.current=key;void request("models");},[draft.base_url,draft.api_key,draft.protocol,form.editing,scene,busy,saving]);
 const focused=profiles.find(p=>form.field==="model:"+p.model_id);
 // Only explicitly configured models are checked, sequentially and once per
 // connection/model/parameter identity. Cursor movement is not a test action.
 const observed=profiles.find(p=>!connectionAttempts.current.has(identity(form.values,p)));
 useEffect(()=>{if(scene||form.editing||busy||saving||!ready(draft)||!observed)return;const key=identity(draft,observed);if(connectionAttempts.current.has(key))return;const timer=setTimeout(()=>{connectionAttempts.current.add(key);void request("connect",observed);},600);return()=>clearTimeout(timer);},[scene,form.editing,busy,saving,draft.base_url,draft.api_key,draft.protocol,observed]);
 useEffect(()=>{if(initialModels){form.select(baseFields.length+profiles.length);setScene("add");}if(initialTest&&profiles[0])void request("probe",profiles[0]);},[]);
 const invalidateConnection=(next:Connection)=>{if(connection(next)===connection(form.values))return;cancel();setSnapshot(null);setCatalog([]);setModelIssue(null);setProfiles(items=>items.map(p=>({...p,protocol:next.protocol,model_receipt:undefined,probe_receipt:undefined,deployment:undefined,verification:undefined,context_source:"user"})));};
 const choosePreset=(id:string)=>{const p=presets.find(p=>p.id===id);if(!p)return;if(presetChosen.current&&id===form.values.preset){cmdInput.setValue("");setScene(null);return;}const next={...form.values,preset:id,name:p.id==="custom"?form.values.name:p.name,base_url:p.base_url,protocol:p.protocol,api_key:""};invalidateConnection(next);presetChosen.current=true;form.reset(next);form.select(3);setScene(null);};
 const leaveForm=()=>{cancel(true);cmdInput.setValue("");feedback.clear();if(!seed&&!local){setPresetOrigin("entry");setScene("presets");}else onDone(false);};
 const save=async(next=draft,items=profilesRef.current,fromJson=false)=>{
  if(savingRef.current)return;
  const missing=!next.name.trim()?"name":!next.base_url.trim()?"base_url":!ready(next)?"api_key":!items.length?"add":"";
  if(missing){
   // Connection entry hands off to the existing model-selection scene.
   // Discovery may already be running: keep it, and never adopt every model
   // or report a missing-model failure before showing its actual result.
   if(missing==="add"&&!fromJson){
    if(form.editing)invalidateConnection(next);
    form.confirm(next);form.select(fields.indexOf("add"));openModels();return;
   }
   const messages={
    name:t({en:"Enter a connection name.",zh:"请填写连接名称。"}),
    base_url:t({en:"Enter the API address; model discovery does not require saving first.",zh:"请填写 API 地址；获取模型无需先保存连接。"}),
    api_key:t({en:"Enter an API Key; model IDs can still be added before saving.",zh:"请填写 API 密钥；保存前仍可先选择或手填模型。"}),
    add:t({en:"Add at least one model; choose candidates or enter model IDs in Add models.",zh:"请至少添加一个模型；从“添加模型…”选择或手填型号。"}),
   };
   const e=Error(messages[missing]);if(fromJson)throw e;
   // A failed whole-form save must not strand the user in a field editor.
   // Stage its live value, then move to the missing input without persisting.
   if(form.editing)invalidateConnection(next);
   form.confirm(next);form.select(fields.indexOf(missing));error(e);return;
  }
  cancel();savingRef.current=true;setSaving(true);
  try{
   const models=items.map(({model_id,protocol,context_window,max_output_tokens,request_preferences,context_source,output_source,model_receipt,probe_receipt})=>({model_id,protocol,context_window,max_output_tokens,request_preferences,context_source,output_source,model_receipt:model_receipt || "",probe_receipt:probe_receipt || ""}));
   const selected=models.find(p=>p.model_id===persisted?.model_id) || models[0]!;
   const result:any=await llmSave(client,{...payload(next),id:persisted?.id || "",name:next.name,...selected,models,expected_revision:persisted?.revision || "",creation_id:persisted?"":creationId.current,preset_id:next.preset});
   if(result.error)throw result.error;
   if(alive.current){
    const message=t({en:"API connection and models saved",zh:"API 连接和模型已保存"});
    if(fromJson){
     if(result.provider)setPersisted(result.provider);
     setProfiles(result.provider?loadedProfiles(result.provider):items);
     returnField.current="advanced";form.reset({...next,api_key:result.provider?"":next.api_key});
     setScene(null);feedback.show(message,{tone:"success"});
    }else onDone(true,message);
   }
  }catch(e:any){if(fromJson)throw e;if(alive.current)error(e);}
  finally{savingRef.current=false;if(alive.current)setSaving(false);}
 };
 const openJson=()=>{cancel();setJsonSeed(configurationJson(form.values,profilesRef.current));cmdInput.setValue("");setScene("json");};
 const openModels=()=>{setChosen([]);setManualCandidates([]);cmdInput.setValue("");feedback.clear();setScene("add");};
 const mergeModels=(ids:string[])=>{setProfiles(items=>{const seen=new Set(items.map(p=>p.model_id)),index=new Map([...manualCandidates,...catalog].map(p=>[p.model_id,p]));return [...items,...[...new Set(ids)].filter(id=>!seen.has(id)).map(id=>index.get(id) || defaultProfile(id,draft.protocol))];});setChosen([]);cmdInput.setValue("");feedback.clear();setScene(null);};
 useEffect(()=>{if(!scene&&returnField.current){const index=fields.indexOf(returnField.current);returnField.current=null;form.select(index<0?fields.length-1:index);}else if(form.fieldIdx>=fields.length)form.select(fields.length-1);},[scene,fields.length,form.fieldIdx]);
 useEffect(()=>{
  if(scene)return;
  if(!form.editing&&!cmdInput.value.startsWith("/")||form.editing&&form.isChoice){onFooter({hidden:true});return()=>onFooter(null);}
  onFooter({kind:form.editing?"normal":"command",secret:form.field==="api_key",cmdInput,suggestions:form.editing?[]:suggestions,suggestionIdx,cmdResult:"",statusText:""});return()=>onFooter(null);
 },[scene,form.editing,form.field,cmdInput,onFooter,t,suggestionIdx]);
 useManagedInput((input,key)=>{
  if(scene)return false;if(savingRef.current)return true;
  if((focused||form.field==="add")&&matchChord("providerModelsRefresh",input,key)){for(const p of focused?[focused]:profiles)connectionAttempts.current.delete(identity(draft,p));void request("models");return true;}
  if(matchChord("escape",input,key)){if(!form.editing&&cmdInput.value.startsWith("/"))cmdInput.setValue("");else if(form.editing)form.cancel();else leaveForm();return true;}
  if(!form.editing&&isCommandMode(cmdInput.value,input)){const idx=Math.min(suggestionIdx,Math.max(0,suggestions.length-1));for(const action of resolveCommandKeys(cmdInput.value,suggestions.length,input,key,suggestions[idx]?.label || "","execute"))applyCommandAction(action,{cmdInput,suggestionLabels:suggestions.map(s=>s.label),suggestionIdx,setSuggestionIdx,runCommand:command=>{cmdInput.setValue("");if(command==="/help"){if(moduleHelp)moduleHelp.open("providers");else setScene("help");}}});return true;}
  if(matchChord("formSave",input,key)){void save();return true;}
  if(form.editing){
   if(matchChord("enter",input,key)){invalidateConnection(draft);form.confirm();feedback.clear();}
   else if(form.isChoice){if(matchChord("left",input,key)||matchChord("right",input,key))form.setChoice(protocols[cycleChoiceIndex(protocols.indexOf(draft.protocol),matchChord("right",input,key)?1:-1,protocols.length)]!);}
   else for(const action of resolveDialogSelectKey(input,key))if(action.type==="text")applyTextOp(action.op,cmdInput);
   return true;
  }
  if(matchChord("up",input,key)||matchChord("shiftTab",input,key))form.move(-1);
  else if(matchChord("down",input,key)||matchChord("tab",input,key))form.move(1);
  else if(focused&&matchChord("delete",input,key)){cancel();setProfiles(items=>items.filter(p=>p.model_id!==focused.model_id));}
  else if(matchChord("enter",input,key)){
   if(form.field==="preset"){setPresetOrigin("field");setScene("presets");}
   else if(form.field==="add")openModels();
   else if(form.field==="advanced")openJson();
   else if(!focused){if(["base_url","api_key","protocol"].includes(form.field))cancel(true);feedback.clear();form.begin();}
  }
  return true;
 },{bindingContext:"form",isActive:!scene,clipboard:form.editing?!form.isChoice&&!saving:true,inputTarget:form.editing?form.field:"commands"});
 if(scene==="help")return <HelpPanel scene="projects" topic="providers" onDismiss={()=>setScene(null)}/>;
 if(scene==="presets")return <SearchableChoiceScene helpTopic="providers" title={t({en:"Choose Provider",zh:"选择 Provider"})} showCount={false} items={presets.map(p=>({id:p.id,title:p.id==="custom"?t({en:"Custom connection",zh:"自定义连接"}):p.name}))} input={cmdInput} onFooter={onFooter} onSelect={choosePreset} onCancel={()=>{cmdInput.setValue("");if(presetOrigin==="entry")onDone(false);else setScene(null);}}/>;
 if(scene==="json")return <DocumentEditorScene title={t({en:"Advanced · configuration JSON",zh:"高级 · 配置 JSON"})}  initialValue={jsonSeed} syntaxLanguage="json" onFooter={onFooter} onCancel={()=>setScene(null)} onSave={async text=>{const next=parseConfiguration(text,form.values,profilesRef.current);await save(next.connection,next.profiles,true);}}/>;
 const stageManual=(text:string)=>{
  const ids=manualModelIds(text);
  setManualCandidates(items=>{const index=new Map([...items,...catalog].map(p=>[p.model_id,p]));return [...new Set([...items.map(p=>p.model_id),...ids])].map(id=>index.get(id)||defaultProfile(id,draft.protocol));});
  setChosen(items=>[...new Set([...items,...ids])]);
 };
 const modelNotice=modelIssue?.connection===connection(draft)?{tone:"warning" as const,text:t(modelIssue.copy)}:snapshot?.credentialsRequired?{tone:"info" as const,text:t({en:"Enter an API Key to load account models; manual IDs remain available.",zh:"填写并确认密钥后获取账号模型；也可先手填型号。"})}:snapshot&&!snapshot.ids.length?{tone:"warning" as const,text:t({en:"The API returned no models; enter model IDs manually or refresh later.",zh:"API 未返回模型；可手填型号或稍后刷新。"})}:snapshot?.reference?{tone:"info" as const,text:t({en:"Public reference list; account access is not verified",zh:"公开参考列表，尚未验证账号可用性"})}:undefined;
 if(scene==="add")return <SearchableChoiceScene helpTopic="providers" title={t({en:"Add models",zh:"添加模型"})} items={[...new Map([...manualCandidates,...catalog].map(p=>[p.model_id,p])).values()].map(p=>({id:p.model_id,title:p.model_id,disabled:profiles.some(saved=>saved.model_id===p.model_id),hint:profiles.some(saved=>saved.model_id===p.model_id)?t({en:"already added",zh:"已添加"}):undefined}))} selectedIds={[...profiles.map(p=>p.model_id),...chosen]} inlineEntry={{label:t({en:"Manual model IDs",zh:"手动填写型号"}),onConfirm:stageManual}} input={cmdInput} onFooter={onFooter} busy={busy==="models"} notice={modelNotice} onSelect={id=>setChosen(items=>items.includes(id)?items.filter(p=>p!==id):[...items,id])} onSubmit={text=>{
  const ids=[...chosen,...(text?manualModelIds(text):[])];
  if(!profiles.length&&!ids.length){if(busy==="models")return;throw Error(t({en:"Select a model or enter a model ID before saving this selection.",zh:"请勾选模型或手填型号，再保存本次选择。"}));}
  mergeModels(ids);
 }} onCancel={()=>{if(pendingAction.current==="models")cancel();setChosen([]);cmdInput.setValue("");setScene(null);}}/>;
 const labels:Record<string,string>={preset:t({en:"Provider",zh:"模型服务商"}),name:t({en:"Name",zh:"名称"}),base_url:t({en:"API address",zh:"API 地址"}),api_key:t({en:"API Key",zh:"密钥"}),protocol:t({en:"Protocol",zh:"协议"})};
 const width=fieldLabelWidth(Object.values(labels),Math.max(1,Math.floor(w/3)));
 return <Box flexDirection="column">
  <Text bold>{seed?t({en:"Edit API connection",zh:"编辑 API 连接"}):t({en:"Configure API connection",zh:"配置 API 连接"})}</Text>
  {baseFields.map((field,index)=><FormFieldRow key={field} label={labels[field]!} labelWidth={width} selected={form.fieldIdx===index} editing={form.editing&&form.fieldIdx===index}><SelectionText wrap="truncate-end">{field==="api_key"?(draft.api_key?"•".repeat(Math.min(24,draft.api_key.length)):retained(draft)?t({en:"existing key retained",zh:"保留已有密钥"}):local?t({en:"not required",zh:"不需要"}):t({en:"required",zh:"待填写"})):field==="preset"?(presets.find(p=>p.id===draft.preset)?.name || draft.preset):draft[field] || "—"}</SelectionText></FormFieldRow>)}
  <Text dimColor>{t({en:"Models",zh:"模型"})} ({profiles.length})</Text>
  {profiles.map(p=><NavigableListRow key={p.model_id} selected={form.field==="model:"+p.model_id}><ConnectivityDot state={health[identity(draft,p)]?.state || "unknown"}/><SelectionText wrap="truncate-end">{p.model_id}</SelectionText></NavigableListRow>)}
  {!profiles.length?<Text dimColor>{t({en:"No models added",zh:"尚未添加模型"})}</Text>:null}
  {!form.editing?<InlineNotice notice={modelNotice} wrap="wrap"/>:null}
  <NavigableListRow selected={form.field==="add"}><SelectionText>{t({en:"Add models…",zh:"添加模型…"})}</SelectionText></NavigableListRow>
  <NavigableListRow selected={form.field==="advanced"}><SelectionText>{t({en:"Advanced · configuration JSON",zh:"高级 · 配置 JSON"})}</SelectionText></NavigableListRow>
  {focused&&health[identity(draft,focused)]?.reason?<Text dimColor>{t({en:"Last connection check failed",zh:"最近连接检查失败"})}: {health[identity(draft,focused)]?.reason}</Text>:null}
  <FeedbackMessage feedback={feedback.message}/>
  {saving?<Text dimColor>{t({en:"Saving…",zh:"正在保存…"})}</Text>:null}
 </Box>;
}
