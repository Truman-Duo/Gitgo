import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {geometry} from "../theme/tokens.js";
import {SelectionText} from "./Selection.js";
import React,{memo,useEffect,useRef,useState,useCallback} from "react";
import {Box,ScrollBox,Text,type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput,useScrollInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {memoryList,memorySnapshot,memoryRestore} from "../backend/tools.js";
import {colors} from "../theme/index.js";
import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,standardKey,moveSelection,reconcileSelection} from "../ui/pageContract.js";
type Snapshot={source:string;timestamp:string;path:string;is_dir:boolean};
type Props={client:BackendClient;project:string;cols:number;onDismiss:()=>void;interactive?:boolean};
export const MemoryPanel=memo(function MemoryPanel({client,project,onDismiss,interactive=true}:Props){
 const t=useCopy(),scroll=useRef<ScrollBoxHandle>(null),alive=useRef(true),generation=useRef(0),mutation=useRef(false);
 const [snaps,setSnaps]=useState<Snapshot[]>([]),[selected,setSelected]=useState(0),[loading,setLoading]=useState(true);
 const [focus,setFocus]=useState<"list"|"actions">("list"),[action,setAction]=useState(0);
 const [confirm,setConfirm]=useState<Snapshot|null>(null),[status,setStatus]=useState(""),[busy,setBusy]=useState(false);
 const rows=[{id:0,enabled:!busy},{id:1,enabled:!busy&&Boolean(snaps[selected])}];
 const current=reconcileSelection(rows,action)??0;
 useScrollInput(scroll,interactive);

 const refresh=useCallback(async()=>{
  const seq=++generation.current;
  try {const result:any=await memoryList(client,project);if(alive.current&&seq===generation.current){
   const values=Array.isArray(result)?result:result?.snapshots||[];setSnaps(values);
   setSelected(v=>Math.max(0,Math.min(v,values.length-1)));setLoading(false);
  }}catch(e:any){if(alive.current&&seq===generation.current){setStatus(String(e.message||e));setLoading(false);}}
 },[client,project]);
 useEffect(()=>{alive.current=true;void refresh();return()=>{alive.current=false;generation.current++;};},[refresh]);
 const perform=async(kind:"snapshot"|"restore",snapshot?:Snapshot)=>{
  if(mutation.current)return;mutation.current=true;setBusy(true);setStatus("");
  try {
   if(kind==="snapshot")await memorySnapshot(client,project);
   else if(snapshot)await memoryRestore(client,project,snapshot.timestamp);
   if(alive.current){setStatus(t(kind==="snapshot"?{en:"Snapshot created",zh:"已创建快照"}:{en:"Snapshot restored",zh:"已恢复快照"}));await refresh();}
  }catch(e:any){if(alive.current)setStatus(String(e.message||e));}
  finally{mutation.current=false;if(alive.current)setBusy(false);}
 };
 const menuHelp=useLocalHelpCommand("memory",undefined,interactive);
  useInput((input,key)=>{
    if(menuHelp.handle(input,key))return;
  const command=standardKey(input,key);
  if(command==="back"){if(confirm)setConfirm(null);else onDismiss();return;}
  if(busy||loading)return;
  if(confirm){if(command==="activate"){const saved=confirm;setConfirm(null);void perform("restore",saved);}return;}
  if((command==="next"||command==="previous")){setFocus(v=>v==="list"?"actions":"list");return;}
  if(focus==="actions"){
   if(command==="left"||command==="right")setAction(v=>moveSelection(rows,v,command==="left"?-1:1)??v);
   else if(command==="up")setFocus("list");
   else if(command==="activate"){if(current===0)void perform("snapshot");else if(snaps[selected])setConfirm(snaps[selected]!);}
   return;
  }
  if(command==="up")setSelected(v=>Math.max(0,v-1));
  else if(command==="down"){if(selected>=snaps.length-1)setFocus("actions");else setSelected(v=>v+1);}
  else if(command==="activate"&&snaps[selected]){setAction(1);setFocus("actions");}
 },{isActive:interactive});
 return <PageFrame page={pages.runtime}>
  <Text bold>{t({en:"Memory snapshots",zh:"记忆快照"})} · {project}</Text>
  {confirm?<Text color={colors.warning}>{t({en:"Restore this exact snapshot?",zh:"恢复此快照？"})} {confirm.timestamp}</Text>:null}
  <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
   {loading?<Text dimColor>{t({en:"Loading snapshots…",zh:"正在读取快照…"})}</Text>:!snaps.length?<Text dimColor>{t({en:"No snapshots yet. Select Create snapshot.",zh:"尚无快照，请选择创建快照。"})}</Text>:snaps.map((sn,i)=><SelectionText selected={i===selected} focused={interactive&&focus==="list"} key={sn.timestamp+sn.source}
     >{sn.source}  {sn.timestamp.slice(0,19)}</SelectionText>)}
  </ScrollBox>
  {status?<Text color={colors.warning}>{status}</Text>:null}
  <Box gap={geometry.actions.gap}>{[t({en:"Create snapshot",zh:"创建快照"}),t({en:"Restore selected",zh:"恢复选中快照"})].map((label,i)=><SelectionText selected={interactive&&focus==="actions"&&current===i} variant="block" key={i}
   dimColor={!rows[i]?.enabled}
   >{label}</SelectionText>)}</Box>

 </PageFrame>;
});
