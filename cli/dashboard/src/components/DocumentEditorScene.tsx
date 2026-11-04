import React,{useEffect,useLayoutEffect,useMemo,useRef,useState} from "react";
import {Box,Text,measureElement,type DOMElement} from "@anthropic/ink";
import {TextInput} from "./TextInput.js";
import type {FooterConfig} from "./CommandBar.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord,chordLabel} from "../input/bindings.js";
import {resolveDialogSelectKey} from "../input/overlays/dialogSelect.js";
import {applyTextOp} from "../hooks/useTextInput.js";
import {usePanelSize,colors} from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";
import {documentLayout,documentCaret,moveDocumentLine} from "../input/documentLayout.js";

/** Full-body source editor. Caret/rendering share a lossless wrap map;
 * scrolling never edits the document or moves the caret. */
export function DocumentEditorScene({title,description,initialValue,onSave,onCancel,onFooter,submitLabel,syntaxLanguage}:{
 title:string;description?:string;initialValue:string;onSave:(text:string)=>Promise<void>|void;
 onCancel:()=>void;onFooter:(footer:FooterConfig|null)=>void;submitLabel?:string;syntaxLanguage?:string;
}){
 const t=useCopy(),input=useTextInput(initialValue),fallback=usePanelSize({widthOffset:6,headerRows:5,footerRows:4});
 const body=useRef<DOMElement>(null),[bounds,setBounds]=useState({width:0,height:0});
 // The shell owns the available area. Measure its flex allocation rather
 // than guessing terminal rows minus a fixed number of unrelated UI rows.
 useLayoutEffect(()=>{
  const measure=()=>{if(!body.current)return;const next=measureElement(body.current);
   if(next.width>0&&next.height>0)setBounds(previous=>previous.width===next.width&&previous.height===next.height?previous:next);
  };
  measure();const timer=setTimeout(measure,0);return()=>clearTimeout(timer);
 });
 const w=Math.max(1,Math.floor(bounds.width||fallback.w)),contentH=Math.max(1,Math.floor(bounds.height||fallback.contentH));
 // Reserve one physical cell for a caret immediately after a full visual row.
 const layout=useMemo(()=>documentLayout(input.value,Math.max(1,w-1)),[input.value,w]);
 const caret=documentCaret(layout.points,input.cursor),maximum=Math.max(0,layout.rows.length-contentH);
 const [top,setTop]=useState(0),topRef=useRef(top),[navigation,setNavigation]=useState(0);
 topRef.current=Math.max(0,Math.min(maximum,top));
 const scroll=(delta:number)=>{const next=Math.max(0,Math.min(maximum,topRef.current+delta));topRef.current=next;setTop(next);};
 useLayoutEffect(()=>{
  const start=Math.max(0,Math.min(maximum,topRef.current));
  const next=caret.row<start?caret.row:caret.row>=start+contentH?Math.max(0,caret.row-contentH+1):start;
  topRef.current=next;setTop(previous=>previous===next?previous:next);
 },[input.value,input.cursor,w,contentH,navigation]);
 const [error,setError]=useState(""),[saving,setSaving]=useState(false),savingRef=useRef(false),alive=useRef(true);
 useEffect(()=>{alive.current=true;onFooter({hidden:true});return()=>{alive.current=false;onFooter(null);};},[onFooter]);
 const submit=async()=>{if(savingRef.current)return;savingRef.current=true;setSaving(true);setError("");
  try{await onSave(input.materialize());}catch(e:any){if(alive.current)setError(e?.message || String(e));}
  finally{savingRef.current=false;if(alive.current)setSaving(false);}
 };
 useManagedInput((text,key)=>{
  if(key.wheelUp||key.wheelDown){scroll(key.wheelUp?-3:3);return true;}
  if(savingRef.current)return true;
  if(matchChord("escape",text,key)){onCancel();return true;}
  if(matchChord("formSave",text,key)){void submit();return true;}
  setNavigation(n=>n+1);
  if(matchChord("enter",text,key)||matchChord("shiftEnter",text,key))input.insert("\n");
  else if(matchChord("tab",text,key))input.insert("  ");
  else if(matchChord("up",text,key)||matchChord("down",text,key))input.moveCursorTo(moveDocumentLine(layout.points,input.cursor,matchChord("up",text,key)?-1:1));
  else if(key.pageUp||key.pageDown)input.moveCursorTo(moveDocumentLine(layout.points,input.cursor,(key.pageUp?-1:1)*Math.max(1,contentH-1)));
  else if(matchChord("ctrlLeft",text,key)||matchChord("altB",text,key))input.moveWord(-1);
  else if(matchChord("ctrlRight",text,key)||matchChord("altF",text,key))input.moveWord(1);
  else if(matchChord("ctrlK",text,key))input.killToEnd();
  else if(matchChord("ctrlU",text,key))input.killToStart();
  else if(matchChord("ctrlW",text,key))input.killWordBack();
  else if(matchChord("ctrlY",text,key))input.yank();
  else if(text&&!key.ctrl&&!key.meta&&(!key.leftArrow&&!key.rightArrow&&!key.home&&!key.end&&!key.backspace&&!key.delete))input.insert(text.replace(/\r\n?/g,"\n"));
  else for(const action of resolveDialogSelectKey(text,key))if(action.type==="text")applyTextOp(action.op,input);
  return true;
 // This standard control owns its document viewport. The shell's passive
 // useScrollInput (+50) must not consume its wheel/page events first.
 },{bindingContext:"form",priority:100,clipboard:!saving,inputTarget:"document"});
 return <Box flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
  <Box flexShrink={0}><Text bold wrap="truncate-end">{title}</Text></Box>{description?<Box flexShrink={0}><Text dimColor wrap="truncate-end">{description}</Text></Box>:null}
  <Box ref={body} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden"><TextInput value={input.value} cursorOffset={input.cursor} maxWidth={w} focus={!saving} syntaxLanguage={syntaxLanguage} document={{rows:layout.rows,caret,top:topRef.current,height:contentH}}/></Box>
  {error?<Box flexShrink={0}><Text color={colors.danger} wrap="truncate-end">{error}</Text></Box>:null}
  <Box flexShrink={0} height={1}>{saving?<Text dimColor wrap="truncate-end">{t({en:"Saving…",zh:"正在保存…"})}</Text>:null}</Box>
 </Box>;
}
