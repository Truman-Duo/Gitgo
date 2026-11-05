import React, {useEffect,useId, useMemo, useRef, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import {SelectionRow, SelectionText, SuggestionRow} from "./Selection.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import {useManagedInput} from "../input/runtime.js";
import {resolveDialogSelectKey} from "../input/overlays/dialogSelect.js";
import {applyTextOp,useTextInput, type UseTextInputReturn} from "../hooks/useTextInput.js";
import {useListNavigation} from "../hooks/useListNavigation.js";
import {matchChord} from "../input/bindings.js";
import type {FooterConfig} from "./CommandBar.js";
import {FormFieldRow} from "./FormFieldRow.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {InlineNotice,type InlineNoticeValue} from "./InlineNotice.js";
import {geometry} from "../theme/tokens.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import type {HelpTopic} from "../ui/moduleHelp.js";

export type ChoiceItem = {id: string; title: string; hint?: string; disabled?: boolean; alwaysVisible?: boolean};

/** Content or bounded inline choice list. Search uses the shared NormalBar;
 * selection is bounded independently of the window. No private input or styles.
 */
export function SearchableChoiceScene({title, items, input, onFooter, onSelect, onCancel, onSubmit,inlineEntry, busy=false, description, notice, currentId, selectedIds,presentation="content",showCount=true,helpTopic}: {
  title: string; items: ChoiceItem[]; input: UseTextInputReturn; busy?: boolean;
  description?: string; notice?: InlineNoticeValue; currentId?:string;
  selectedIds?: readonly string[];
  presentation?: "content" | "inline";
  showCount?: boolean;
  helpTopic?: HelpTopic;
  onFooter: (footer: FooterConfig | null) => void; onSelect: (id: string) => void; onCancel: () => void; onSubmit?:(inlineText?:string)=>void;
  inlineEntry?:{label:string;onConfirm:(value:string)=>void};
}) {
  const t = useCopy();
  const {contentH} = usePanelSize({headerRows: 9, footerRows: 5});
  const entryId=useId(),manual=useTextInput(""),[editing,setEditing]=useState(false);
  const help=useLocalHelpCommand(helpTopic||"providers",onFooter,Boolean(helpTopic)&&!editing&&!input.value&&!busy);
  const feedback=useFeedback({kind:"page",id:entryId});
  const filtered = useMemo(() => [...items.filter(item => item.alwaysVisible || `${item.title} ${item.hint || ""}`.toLowerCase().includes(input.value.toLowerCase())),...(inlineEntry?[{id:entryId,title:inlineEntry.label,alwaysVisible:true}]:[])], [items, input.value,inlineEntry,entryId]);
  const [selected, setSelected] = useState("");
  const viewport = useRef(null);
  const active = filtered.find(item => item.id === selected && !item.disabled)?.id || filtered.find(item => item.id === currentId && !item.disabled)?.id || filtered.find(item => !item.disabled)?.id || "";
  const move = useListNavigation(filtered.map(item => ({id: item.id, enabled: !item.disabled})), active, setSelected, viewport);
  useEffect(() => {setSelected("");}, [input.value]);
  useEffect(() => {
    if(help.active)return;
    onFooter({kind: "normal", cmdInput: editing?manual:input, preserveStatus:presentation==="inline", statusText:"",
              suggestions: [], suggestionIdx: 0, cmdResult: ""});
    return () => onFooter(null);
  }, [input,manual,editing, onFooter,presentation,help.active]);
  useManagedInput((text, key) => {
    if(help.handle(text,key))return true;
    const attempt=(run:()=>void)=>{try{run();feedback.clear();}catch(e:any){feedback.show(e?.message || String(e),{tone:"error"});}};
    if(onSubmit&&matchChord("formSave",text,key)){attempt(()=>onSubmit(editing?manual.materialize():undefined));return true;}
    if(editing){
      if(matchChord("escape",text,key)){manual.setValue("");setEditing(false);return true;}
      if(matchChord("shiftEnter",text,key)){manual.insert("\n");return true;}
      if(matchChord("enter",text,key)){attempt(()=>{inlineEntry?.onConfirm(manual.materialize());manual.setValue("");setEditing(false);});return true;}
      for(const action of resolveDialogSelectKey(text,key))if(action.type==="text")applyTextOp(action.op,manual);
      return true;
    }
    for (const action of resolveDialogSelectKey(text, key)) {
      if (action.type === "dismiss") onCancel();
      else if (action.type === "move") move(action.delta);
      else if (action.type === "confirm" && active){if(inlineEntry&&active===entryId)setEditing(true);else onSelect(active);}
      else if (action.type === "text") applyTextOp(action.op, input);
    }
    return true;
  },{bindingContext:onSubmit||inlineEntry?"form":undefined,inputTarget:editing?"manual-ids":help.active?"help-command":"search"});
  const count = Math.min(presentation==="inline"?geometry.choice.maxVisible:Infinity,Math.max(1, contentH - 1 - Number(showCount) - Number(presentation==="inline") - Number(busy) - Number(Boolean(description)) - Number(Boolean(notice))));
  const index = Math.max(0, filtered.findIndex(item => item.id === active));
  const start = Math.max(0, Math.min(index - Math.floor(count / 2), filtered.length - count));
  return <Box flexDirection="column" minHeight={0}>
    <Text bold>{title}</Text>
    {description ? <Text dimColor wrap="truncate-end">{description}</Text> : null}
    <InlineNotice notice={notice}/>
    {busy ? <Text dimColor>{t({en:"Loading…",zh:"正在读取…"})}</Text> : null}
    {filtered.slice(start, start + count).map(item => item.id===entryId?<FormFieldRow key={item.id} label={item.title} suffix="" selected={item.id===active} editing={editing}><SelectionText>{editing?manual.value:"—"}</SelectionText></FormFieldRow>:presentation==="inline"?<SuggestionRow tone="neutral" key={item.id} selected={item.id===active} disabled={item.disabled} label={<>{item.title}{item.id===currentId?t({en:" · current",zh:" · 当前"}):""}</>} description={item.hint}/>:<SelectionRow key={item.id} selected={item.id === active} disabled={item.disabled}>
      <SelectionText wrap="truncate-end">{selectedIds && !item.alwaysVisible ? (selectedIds.includes(item.id) ? "[x] " : "[ ] ") : ""}{item.title}{item.id === currentId ? t({en:" · current",zh:" · 当前"}) : ""}{item.hint ? ` · ${item.hint}` : ""}</SelectionText>
    </SelectionRow>)}
    {!filtered.length ? <Text dimColor>{t({en:"No matches",zh:"无匹配项"})}</Text> : null}
    <FeedbackMessage feedback={feedback.message}/>
    {showCount?<Text dimColor>{filtered.length ? index + 1 : 0}/{filtered.length}</Text>:null}
  </Box>;
}
