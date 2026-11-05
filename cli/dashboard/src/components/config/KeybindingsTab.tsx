import {useLocalHelpCommand} from "../../hooks/useLocalHelpCommand.js";
import {SelectionText} from "../Selection.js";
import React, {memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput, useScrollInput, useEffectiveBindings, useConfigureBindings, useBindingRevision} from "../../input/runtime.js";
import {BINDING_DEFINITIONS, InputBindingRegistry, bindingFromEvent, bindingLabel, presetSettings,
  type BindingSettings, type BindingSpec} from "../../input/router.js";
import {matchChord} from "../../input/bindings.js";
import {configGet, configSet} from "../../backend/tools.js";
import {notifyGeneralConfigChanged} from "../../hooks/useGeneralConfig.js";
import {useCopy} from "../../ui/PageFrame.js";
import {useFeedback} from "../../hooks/useFeedback.js";
import {colors, usePanelSize} from "../../theme/index.js";
import type {ConfigTabProps} from "./types.js";

const BindingRow = memo(function BindingRow({label, width, selected, selectedRow}: {
  label:string; width:number; selected:boolean;
  selectedRow:React.RefObject<DOMElement|null>;
}) {
  return <Box ref={selected?selectedRow:undefined} flexDirection="row" width={width} flexShrink={0}>
    <SelectionText selected={selected} wrap="truncate-end">{label}</SelectionText>
  </Box>;
});

/** Reads the dispatcher registry. No parallel display map or invented presets. */
export const KeybindingsTab = memo(function KeybindingsTab({client, report, onFooter, onStatusUpdate, shell, contentFocused}: ConfigTabProps) {
  const t = useCopy(), registry = useEffectiveBindings(), configure = useConfigureBindings();
  const {w,h} = usePanelSize({widthOffset:6}), revision = useBindingRevision();
  const labels = useMemo(()=>BINDING_DEFINITIONS.map(item=>t(item.label)+" · "+registry.displayLabel(item.id)),[t,registry,revision]);
  const [settings, setSettings] = useState<BindingSettings>({version:1,overrides:{}});
  const [loaded,setLoaded] = useState(false), [saving,setSaving] = useState(false), [row,setRow] = useState(0);
  const [mode,setMode] = useState<"list"|"capture">("list");
  const [sawInput,setSawInput] = useState(false);
  const [candidate,setCandidate] = useState<BindingSpec|null>(null);
  const feedback = useFeedback({kind: "page", id: "config/keybindings"});
  const message = feedback.text, setMessage = feedback.show;
  const alive = useRef(true), mutation = useRef(false), scroll = useRef<ScrollBoxHandle>(null);
  const selectedRow = useRef<DOMElement>(null);
  const action = BINDING_DEFINITIONS[row-1];
  useScrollInput(scroll, contentFocused && mode === "list");
  useLayoutEffect(()=>{
    if(!contentFocused)return;
    if(row===0)scroll.current?.scrollTo(0);
    else if(selectedRow.current)scroll.current?.scrollIntoView(selectedRow.current);
  },[row,w,h,mode,message,contentFocused,revision]);
  const menuHelp=useLocalHelpCommand("keybindings",onFooter,contentFocused&&mode==="list");
  useEffect(() => {
    alive.current=true;
    configGet(client).then((value:any)=>{
      if(!alive.current)return;
      const next = value?.keyboard_bindings || presetSettings(value?.keyboard_preset === "portable" ? "portable" : "standard");
      new InputBindingRegistry().configure(next);
      setSettings(next); setLoaded(true);
    }).catch(error=>{if(alive.current)setMessage(t({en:`Could not load bindings: ${error}`,zh:`无法读取绑键：${error}`}));});
    return ()=>{alive.current=false;};
  },[client,t]);
  useEffect(()=>{report({sub:mode!=="list",fullscreen:false});},[mode,report]);
  useEffect(()=>{
    if(menuHelp.active)return;onFooter({hidden:true});return ()=>onFooter(null);},[onFooter,menuHelp.active]);
  useEffect(()=>{onStatusUpdate?.(message);},[message,onStatusUpdate]);
  useEffect(()=>{
    if(mode==="list" || sawInput)return;
    const timer=setTimeout(()=>setMessage(t({en:"No key detected yet. The terminal or OS may intercept it. Esc cancels.",zh:"尚未检测到按键；终端或系统可能截获了组合键。Esc 取消。"})),5000);
    return ()=>clearTimeout(timer);
  },[mode,sawInput,t]);
  const problem = (error:unknown) => {
    const text=String(error);
    if(text.includes("INPUT_BINDING_CONFLICT")){
      const ids=text.split(": ").at(-1)!.split(" / ");
      const names=ids.map(id=>BINDING_DEFINITIONS.find(d=>d.id===id)).filter(Boolean).map(d=>t(d!.label));
      return t({en:`Key already used in an overlapping context: ${names.join(" / ")}`,zh:`此键与同时可用的动作冲突：${names.join(" / ")}`});
    }
    if(text.includes("INPUT_BINDING_TEXT_CONFLICT"))return t({en:"A plain character would replace normal typing. Use Ctrl, Alt or Super for a common action.",zh:"普通字符会影响正常打字；通用动作请使用 Ctrl、Alt 或 Super 组合键。"});
    return t({en:`Could not use this binding: ${text}`,zh:`无法使用此绑键：${text}`});
  };
  const save = useCallback(async(next:BindingSettings)=>{
    if(!loaded || mutation.current)return;
    try{new InputBindingRegistry().configure(next);}catch(error){setMessage(problem(error));return;}
    mutation.current=true;setSaving(true);
    try{
      await configSet(client,"keyboard_bindings",next);
      if(alive.current){configure(next);setSettings(next);setMode("list");setCandidate(null);setMessage(t({en:"Saved; active immediately",zh:"已保存并立即生效"}));}
      notifyGeneralConfigChanged();
    }catch(error){if(alive.current)setMessage(t({en:`Save failed; previous bindings retained: ${error}`,zh:`保存失败，保留原绑键：${error}`}));}
    finally{mutation.current=false;if(alive.current)setSaving(false);}
  },[client,loaded,configure,t]);
  const cancel=()=>{setMode("list");setCandidate(null);setMessage("");};
  // Capture before remapping, clipboard and modal navigation. Paste stays text.
  useInput((input,key)=>{
    if(key.escape){cancel();return;}
    if(saving)return;
    if(mode==="capture" && candidate && key.return && !key.ctrl && !key.meta && !key.shift && !key.super){
      if(action)void save({version:1,overrides:{...settings.overrides,[action.id]:candidate}});
      return;
    }
    setSawInput(true);
    const detected=bindingFromEvent({input,key});
    if(!detected){setMessage(t({en:"This event is text/paste or an unsupported key; no binding changed.",zh:"此事件是文字、粘贴或尚不支持的键，没有修改绑键。"}));return;}
    if(mode==="capture" && action){
      try{new InputBindingRegistry().configure({version:1,overrides:{...settings.overrides,[action.id]:detected}});}
      catch(error){setCandidate(null);setMessage(problem(error));return;}
    }
    setCandidate(detected);
    setMessage(t({en:`Received ${bindingLabel(detected)}`,zh:`实际收到 ${bindingLabel(detected)}`}));
  },{isActive:contentFocused && mode!=="list",raw:true,clipboard:false,priority:20000});
  useInput((input,key)=>{
    if(menuHelp.handle(input,key))return;
    if(!contentFocused || mode!=="list")return false;
    if(saving)return;
    if(matchChord("escape",input,key)){shell.leaveContent();return;}
    if(!loaded || key.paste)return;
    if(matchChord("up",input,key)){
      if(row===0){
        if(scroll.current && (scroll.current.getScrollTop()!==0 || scroll.current.getPendingDelta()!==0))scroll.current.scrollTo(0);
      }else setRow(v=>Math.max(0,v-1));
    }
    else if(matchChord("down",input,key)){
      if(row===BINDING_DEFINITIONS.length){
        const view=scroll.current;
        if(view && selectedRow.current && view.getScrollTop()+view.getPendingDelta()<Math.max(0,view.getScrollHeight()-view.getViewportHeight()))
          view.scrollIntoView(selectedRow.current);
      }else setRow(v=>Math.min(BINDING_DEFINITIONS.length,v+1));
    }
    else if(matchChord("delete",input,key) && action){const overrides={...settings.overrides};delete overrides[action.id];void save({version:1,overrides});}
    else if(matchChord("enterNoShift",input,key)){
      if(row===0)void save({version:1,overrides:{}});
      else {setCandidate(null);setSawInput(false);setMessage(t({en:"Press the replacement key. Esc cancels.",zh:"请按新的绑定键；Esc 取消。"}));setMode("capture");}
    }
  });
  return <Box flexDirection="column" flexGrow={1} minHeight={0}>
    <SelectionText selected={contentFocused&&row===0} highlight={false}>{t({en:"Restore all defaults",zh:"恢复全部默认绑键"})}</SelectionText>

    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {BINDING_DEFINITIONS.map((item,index)=><BindingRow key={item.id} label={labels[index]}
        width={w} selected={contentFocused&&row===index+1} selectedRow={selectedRow}/>)}
    </ScrollBox>
    {message?<Text color={colors.warning}>{message}</Text>:null}

  </Box>;
});
