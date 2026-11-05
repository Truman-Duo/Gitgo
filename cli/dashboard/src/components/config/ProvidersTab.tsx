import React,{memo,useEffect,useMemo,useRef,useState} from "react";
import {Box,Text} from "@anthropic/ink";
import {ProviderEditor} from "./ProviderEditor.js";
import {SelectionText} from "../Selection.js";
import {NavigableListRow} from "../NavigableListRow.js";
import {FormFieldRow} from "../FormFieldRow.js";
import {FeedbackMessage} from "../FeedbackMessage.js";
import {useCopy} from "../../ui/PageFrame.js";
import {useManagedInput,useContentViewport,ScrollableInputLayer} from "../../input/runtime.js";
import {useListNavigation} from "../../hooks/useListNavigation.js";
import {useLLMConfig,type LLMProvider} from "../../hooks/useLLMConfig.js";
import {useFeedback} from "../../hooks/useFeedback.js";
import {matchChord,chordLabel} from "../../input/bindings.js";
import {cycleChoiceIndex} from "../../input/choice.js";
import {isCommandMode,resolveCommandKeys,applyCommandAction} from "../../input/commandInput.js";
import {colors,truncate,usePanelSize} from "../../theme/index.js";
import {geometry} from "../../theme/tokens.js";
import type {ConfigTabProps} from "./types.js";
import type {Suggestion} from "../CommandBar.js";
import {ModuleHelpBoundary,useModuleHelp} from "../ModuleHelpBoundary.js";

type Row={id:string;owner:string;slot?:number};
type RouteEdit={owner:string;slot?:number;value:string;revision:string};

/** One provider page; each root owns exactly its own two ordered backup slots. */
export const ProvidersTab=memo(function ProvidersTab(props:ConfigTabProps){return <ModuleHelpBoundary onFooter={props.onFooter}>{onFooter=><ScrollableInputLayer><ProvidersTabBody {...props} onFooter={onFooter}/></ScrollableInputLayer>}</ModuleHelpBoundary>;});
function ProvidersTabBody({client,project,cmdInput,onFooter,report,shell,contentFocused}:ConfigTabProps){
 const t=useCopy(),{w}=usePanelSize({widthOffset:4});
 const {providers,activeProvider,failoverEnabled,failoverRoutes,failoverRevision,configureFailover,error,health,loading,switchProvider,deleteProvider,fetchStatus}=useLLMConfig(client,project);
 const [editing,setEditing]=useState(false),[seed,setSeed]=useState<LLMProvider>(),[local,setLocal]=useState(false),[startModels,setStartModels]=useState(false),[startProbe,setStartProbe]=useState(false);
 const [selected,setSelected]=useState("enabled"),[routeEdit,setRouteEdit]=useState<RouteEdit|null>(null),[saving,setSaving]=useState(false),mutation=useRef(false);
 const [suggestionIdx,setSuggestionIdx]=useState(0),feedback=useFeedback({kind:"page",id:`config/providers:${project}`}),viewport=useContentViewport();
 const rows=useMemo<Row[]>(()=>[{id:"enabled",owner:""},...providers.flatMap(p=>[{id:"api:"+p.id,owner:p.id},{id:`backup:${p.id}:0`,owner:p.id,slot:0},{id:`backup:${p.id}:1`,owner:p.id,slot:1}])],[providers]);
 const current=rows.find(row=>row.id===selected)||rows[0]!;
 const move=useListNavigation(rows.map(row=>({id:row.id,enabled:true})),current.id,setSelected,viewport || {current:null});
 const owner=providers.find(p=>p.id===current.owner),backup=current.slot===undefined?undefined:failoverRoutes[current.owner]?.[current.slot];
 const selectedProvider=providers.find(p=>p.id===(backup||owner?.id||activeProvider));
 const help=useModuleHelp();
 const activate=async(id:string)=>{
  if(mutation.current)return;
  if(id===activeProvider){feedback.show(t({en:"Already the current API",zh:"已是当前 API"}),{tone:"info"});return;}
  mutation.current=true;setSaving(true);feedback.clear();
  try{if(await switchProvider(id))feedback.show(t({en:`Switched to ${providers.find(p=>p.id===id)?.name||id}`,zh:`已切换至 ${providers.find(p=>p.id===id)?.name||id}`}),{tone:"success"});}
  finally{mutation.current=false;setSaving(false);}
 };
 useEffect(()=>{void fetchStatus();},[fetchStatus]);
 useEffect(()=>{report({sub:false,fullscreen:editing});},[editing,report]);
 const open=(p?:LLMProvider,kind:"remote"|"local"="remote",models=false,probe=false)=>{setSeed(p);setLocal(kind==="local");setStartModels(models);setStartProbe(probe);cmdInput.setValue("");setEditing(true);};
 const runCommand=(command:string)=>{
  const name=command.replace(/^[:\/]\s*/,"").trim();cmdInput.setValue("");feedback.clear();
  if(name==="help")help?.open("providers");else if(name==="new")open();else if(name==="local")open(undefined,"local");
  else if(name==="edit"&&selectedProvider)open(selectedProvider);
  else if(name==="models"&&selectedProvider)open(selectedProvider,"remote",true);
  else if(name==="test"&&selectedProvider)open(selectedProvider,"remote",false,true);
  else if(name==="delete"&&selectedProvider)void deleteProvider(selectedProvider.id);
  else if(name==="switch"&&selectedProvider)void activate(selectedProvider.id);
  else feedback.show(t({en:"Select an API connection first",zh:"请先选择 API 连接"}),{tone:"warning"});
 };
 const suggestions:Suggestion[]=cmdInput.value.startsWith("/")?[
  {label:t({en:"/help",zh:"/help"}),description:t({en:"Help",zh:"帮助"}),inputMode:"execute" as const},
  {label:t({en:"/new",zh:"/new"}),description:t({en:"Create provider",zh:"创建 Provider"}),inputMode:"execute" as const},
  {label:t({en:"/local",zh:"/local"}),description:t({en:"Create local API provider",zh:"创建本地 API Provider"}),inputMode:"execute" as const},
  {label:t({en:"/edit",zh:"/edit"}),description:t({en:"Edit selected provider",zh:"编辑所选 Provider"}),inputMode:"execute" as const},
  {label:t({en:"/delete",zh:"/delete"}),description:t({en:"Delete selected provider",zh:"删除所选 Provider"}),inputMode:"execute" as const},
 ].filter(item=>item.label.startsWith(cmdInput.value.toLowerCase())):[];
 useEffect(()=>{
  if(editing)return;
  // This page owns fixed commands: retain its CommandBar even with an empty
  // buffer. Only header focus and an active enum edit suspend that owner.
  if(!contentFocused||routeEdit){onFooter({hidden:true});return()=>onFooter(null);}
  onFooter({kind:"command",cmdInput,suggestions,suggestionIdx,cmdResult:"",statusText:saving?t({en:"Applying…",zh:"正在应用…"}):""});return()=>onFooter(null);
 },[editing,contentFocused,routeEdit,cmdInput,onFooter,suggestionIdx,error,feedback.text,t,saving]);
 const beginRoute=()=>setRouteEdit({owner:current.owner,slot:current.slot,value:current.id==="enabled"?(failoverEnabled?"on":"off"):backup||"",revision:failoverRevision});
 const saveRoute=async()=>{
  if(!routeEdit||mutation.current)return;
  mutation.current=true;setSaving(true);
  const edit=routeEdit,order=edit.slot===undefined?undefined:[...(failoverRoutes[edit.owner]||[])];
  if(order&&edit.slot!==undefined)order[edit.slot]=edit.value;
  const ok=await configureFailover(edit.slot===undefined?edit.value==="on":failoverEnabled,order?.filter(Boolean),edit.owner||undefined,edit.revision);
  mutation.current=false;setSaving(false);
  if(ok){setRouteEdit(null);feedback.show(t({en:"Failover configuration saved",zh:"备用配置已保存"}),{tone:"success"});}
  else feedback.show(t({en:"Save failed; refresh and check the route before retrying",zh:"保存失败，请刷新并核对备用关系后重试"}),{tone:"error"});
 };
 useManagedInput((input,key)=>{
  if(!contentFocused||editing)return false;if(mutation.current)return true;
  if(routeEdit){
   if(matchChord("escape",input,key)){setRouteEdit(null);return true;}
   if(matchChord("left",input,key)||matchChord("right",input,key)){
    const other=routeEdit.slot===undefined?[]:(failoverRoutes[routeEdit.owner]||[]).filter((_,i)=>i!==routeEdit.slot);
    const choices=routeEdit.slot===undefined?["off","on"]:["",...providers.filter(p=>p.id!==routeEdit.owner&&!other.includes(p.id)).map(p=>p.id)];
    setRouteEdit({...routeEdit,value:choices[cycleChoiceIndex(choices.indexOf(routeEdit.value),matchChord("right",input,key)?1:-1,choices.length)]!});
   }else if(matchChord("enter",input,key))void saveRoute();
   return true;
  }
  if(matchChord("escape",input,key)){if(cmdInput.value)cmdInput.setValue("");else shell.leaveContent();return true;}
  if(isCommandMode(cmdInput.value,input)){
   const index=Math.min(suggestionIdx,Math.max(0,suggestions.length-1));
   for(const action of resolveCommandKeys(cmdInput.value,suggestions.length,input,key,suggestions[index]?.label || "","execute"))applyCommandAction(action,{cmdInput,suggestionLabels:suggestions.map(s=>s.label),suggestionIdx,setSuggestionIdx,runCommand});return true;
  }
  if(matchChord("up",input,key)||matchChord("shiftTab",input,key))move(-1);
  else if(matchChord("down",input,key)||matchChord("tab",input,key))move(1);
  else if(matchChord("enter",input,key)){
   if(current.id==="enabled"||current.slot!==undefined)beginRoute();
   else if(owner)void activate(owner.id);
  }
  return true;
 },{isActive:contentFocused&&!editing,clipboard:!routeEdit,inputTarget:routeEdit?"route-edit":"provider-commands"});
 if(editing)return <ProviderEditor client={client} project={project} cmdInput={cmdInput} onFooter={onFooter} seed={seed} local={local} initialModels={startModels} initialTest={startProbe} onDone={(saved,message)=>{setEditing(false);cmdInput.setValue("");feedback.show(message || "");if(saved)void fetchStatus();}}/>;
 const routeStatus=(id:string)=>{const state=health[id]?.breaker_state;const labels:Record<string,string>={closed:t({en:"routing enabled",zh:"路由已启用"}),open:t({en:"paused after failures",zh:"失败后暂时停用"}),half_open:t({en:"checking recovery",zh:"正在检查恢复"}),unavailable:t({en:"runtime status unavailable",zh:"运行时状态不可用"})};return state?" · "+(labels[state]||state):"";};
 const enabledLabel=t({en:"Automatic failover",zh:"自动备用切换"});
 return <Box flexDirection="column" minHeight={0}>
  <FormFieldRow label={enabledLabel} selected={contentFocused&&current.id==="enabled"} editing={Boolean(routeEdit&&routeEdit.slot===undefined)}><SelectionText>{routeEdit&&routeEdit.slot===undefined?(routeEdit.value==="on"?t({en:"on",zh:"开启"}):t({en:"off",zh:"关闭"})):failoverEnabled?t({en:"on",zh:"开启"}):t({en:"off",zh:"关闭"})}</SelectionText></FormFieldRow>
  {loading?<Text dimColor>{t({en:"Loading…",zh:"正在读取…"})}</Text>:null}
  {!loading&&!providers.length?<Text dimColor>{t({en:"No API connections; use /new to add one",zh:"尚无 API 连接，输入 /new 添加"})}</Text>:null}
  {providers.map(p=><Box key={p.id} flexDirection="column" flexShrink={0}>
   <NavigableListRow selected={contentFocused&&current.id==="api:"+p.id}><Text color={p.id===activeProvider?colors.success:undefined} dimColor={p.id!==activeProvider}>{p.id===activeProvider?"● ":"○ "}</Text><SelectionText wrap="truncate-end">{p.name}{routeStatus(p.id)}</SelectionText></NavigableListRow>
   {[0,1].map(slot=>{const edit=routeEdit?.owner===p.id&&routeEdit.slot===slot;const id=edit?routeEdit!.value:failoverRoutes[p.id]?.[slot];const label=providers.find(candidate=>candidate.id===id)?.name || t({en:"not configured",zh:"未配置"});return <Box key={slot} paddingLeft={geometry.field.gap} flexDirection="column" flexShrink={0}><FormFieldRow label={truncate(label,Math.max(1,w-geometry.field.gap))} suffix="" muted={!failoverEnabled} selected={contentFocused&&current.id===`backup:${p.id}:${slot}`} editing={edit}>{null}</FormFieldRow></Box>;})}
  </Box>)}
  <FeedbackMessage feedback={feedback.message}/>{error?<Text color={colors.danger}>{error}</Text>:null}
 </Box>;
}
