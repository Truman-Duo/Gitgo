import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages} from "../ui/pageContract.js";

import {MarkdownText} from "./MarkdownText.js";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, ScrollBox, Text, measureElement, type DOMElement, type ScrollBoxHandle } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import {
  btwAsk, cancelBtw, saveBtwNote, sendRuntimeFeedback, stopProcess, type LessonProposalRef,
} from "../backend/tools.js";
import { initStreamState, reduceStreamEvent, type StreamState } from "../daemon/streamReducer.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import {scrollViewport} from "../input/scrollViewport.js";
import { useTextInput } from "../hooks/useTextInput.js";
import { colors } from "../theme/index.js";
import { TextInput } from "./TextInput.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";

type Props = {
  client: BackendClient; project: string; processId: string;
  cols?: number; maxBodyRows?:number; onBrowseParent?: (delta:number)=>number;
  question: string; answer: string; reasoning?: string; sidecarId: string;
  processes: Record<string, ProcessInfo>;
  verbose?: boolean;
  lessonProposal?: LessonProposalRef;
  onDismiss: () => void; onApply: (text: string) => void;
};


export function buildBtwSources(
  processes: Record<string, ProcessInfo>, currentProcessId = "",
): ProcessInfo[] {
  const all = Object.values(processes).filter((item) => !item.archived);
  const roots = all.filter((item) => !item.parent_id)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const latest=new Map<string,ProcessInfo>();
  for(const item of all.filter(item=>!!item.parent_id).sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at))))
    latest.set(item.session_id || item.process_id,item);
  const workers=[...latest.values()];
  const current=all.find(item=>item.process_id===currentProcessId);
  if(current?.parent_id && !workers.some(item=>item.process_id===currentProcessId)){
    const index=workers.findIndex(item=>item.session_id && item.session_id===current.session_id);
    if(index>=0)workers[index]=current;
  }
  const chosenRoots = [
    ...roots.filter((item) => item.process_id === currentProcessId),
    ...roots.slice(0, 1),
  ];
  return [
    ...new Map([...chosenRoots, ...workers].map((item) => [item.process_id, item])).values(),
  ];
}

export function BtwPanel(props: Props) {
  const t=useCopy();
  const width=Math.max(12,(props.cols || process.stdout.columns || 80)-6);
  const body=useRef<ScrollBoxHandle>(null), content=useRef<DOMElement>(null);
  const [bodyRows,setBodyRows]=useState(2),[confirm,setConfirm]=useState(false);
  const maximum=Math.max(2,props.maxBodyRows??10);
  const [selection, setSelection] = useState(0);
  const [focus, setFocus] = useState<"body" | "input" | "choices" | "parent">("body");
  const choices = props.lessonProposal ? [t({en:"Return to review",zh:"返回审阅"})] : [
    t({en:"Close",zh:"关闭"}),t({en:"Keep note",zh:"保留笔记"}),t({en:"Apply at boundary",zh:"边界追加"}),t({en:"Interrupt & apply",zh:"中断并追加"})];

  const [latestQuestion,setLatestQuestion]=useState(props.question);
  const draft = useTextInput("");
  const [answer, setAnswer] = useState(props.answer);
  const [reasoning, setReasoning] = useState(props.reasoning || "");
  const [sidecarId, setSidecarId] = useState(props.sidecarId);
  const [history, setHistory] = useState<Array<{ role: "user" | "assistant"; content: string }>>([]);
  const historyRef = useRef(history);
  const [stream, setStream] = useState<StreamState>(() => initStreamState());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const initialStarted = useRef(false),alive=useRef(true),requestBusy=useRef(false),mutation=useRef(false);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const sources = useMemo(
    () => buildBtwSources(props.processes, props.processId),
    [props.processes, props.processId],
  );
  const [sourceIndex, setSourceIndex] = useState(()=>Math.max(0,sources.findIndex(p=>p.process_id===props.processId)));
  const selectedSources=useMemo(()=>[...new Set([props.processId,...sources.map(s=>s.process_id)].filter(Boolean))],[sources,props.processId]);
  const target=sources[sourceIndex];
  const targetId=target?.process_id||props.processId;
  const targetName=target?.display_name||(target?.parent_id?"B":"A");

  useEffect(() => { historyRef.current = history; }, [history]);

  const ask = useCallback((question: string) => {
    const clean = question.trim();
    if (!clean || busy || requestBusy.current) return;
    requestBusy.current=true;
    const prior = historyRef.current;
    setBusy(true);setBodyRows(2);setFocus("body");
    setLatestQuestion(clean);
    body.current?.scrollTo(0);
    setError("");
    setAnswer("");
    setReasoning("");
    setStream(initStreamState());
    void btwAsk(
      props.client, props.project, clean, sidecarId, prior, selectedSources,
      (raw) => {if(alive.current)setStream((current) => reduceStreamEvent(current, raw as StreamEvent));},
      props.lessonProposal,
    ).then((value: any) => {
      if(!alive.current)return;
      if (value?.pending_decision && value?.decision_process_id) {
        // The canonical process/question queue renders the existing card.
        // Closing this side discussion neither answers nor grants permission.
        props.onDismiss();
        return;
      }
      const next = String(value?.answer || "");
      setAnswer(next);
      setReasoning(String(value?.reasoning_content || ""));
      setSidecarId(String(value?.sidecar_id || sidecarId));
      setHistory((old) => [
        ...old,
        { role: "user", content: clean },
        { role: "assistant", content: next },
      ]);
    }).catch((caught: any) => {
      if(!alive.current)return;
      setError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => {requestBusy.current=false;if(alive.current)setBusy(false);});
  }, [busy, props.client, props.project, props.lessonProposal, selectedSources, sidecarId]);

  useEffect(() => {
    if (initialStarted.current) return;
    initialStarted.current = true;
    ask(props.question);
  }, [ask, props.question]);

  const finishChoice = useCallback(() => {
    if(mutation.current)return;
    const visibleAnswer = answer || stream.text;
    const applyText = `BTW discussion:\nQuestion: ${latestQuestion}\nConclusion: ${visibleAnswer}`;
    if((selection===2||selection===3)&&!confirm){setConfirm(true);return;}
    mutation.current=true;setBusy(true);
    void (async () => {
      if (props.lessonProposal) {
        props.onDismiss(); return;
      } else if (selection === 1) {
        await saveBtwNote(props.client, props.project, sidecarId, applyText);
      } else if (selection === 2 && targetId) {
        await sendRuntimeFeedback(props.client, props.project, targetId, applyText);
      } else if (selection === 2) {
        props.onApply(applyText);
      } else if (selection === 3) {
        if(targetId){
          await stopProcess(props.client,props.project,targetId);
          await sendRuntimeFeedback(props.client,props.project,targetId,applyText);
        }else props.onApply(applyText);
      }
      if(alive.current)props.onDismiss();
    })().catch((caught: any) => {
      mutation.current=false;if(!alive.current)return;
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy(false);
    });
  }, [answer, stream.text, selection, props, sidecarId, latestQuestion, confirm, targetId]);

  const scrollDiscussion=(delta:number)=>{
    if(delta<0){
      const remainder=scrollViewport(body.current,delta);
      if(remainder)props.onBrowseParent?.(remainder);
    }else{
      const remainder=props.onBrowseParent ? props.onBrowseParent(delta) : delta;
      if(remainder)scrollViewport(body.current,remainder);
    }
  };

  useInput((input, key) => {
    if(confirm){
      if(key.escape)setConfirm(false);
      else if(key.leftArrow)setSourceIndex(v=>Math.max(0,v-1));
      else if(key.rightArrow)setSourceIndex(v=>Math.min(sources.length-1,v+1));
      else if(key.return&&!busy)finishChoice();
      return;
    }
    if (key.escape) {
      if (busy && sidecarId) void cancelBtw(props.client, props.project, sidecarId).catch(() => undefined);
      props.onDismiss();
      return;
    }
    if (key.tab) {
      setFocus(value => {
        const regions:typeof focus[]=props.lessonProposal?["body","input","choices"]:["body","input","choices",...(props.onBrowseParent?["parent" as const]:[])];
        return regions[(regions.indexOf(value)+1)%regions.length]!;
      });
      return;
    }
    if (focus === "parent") {
      if(key.upArrow||key.pageUp||key.wheelUp)props.onBrowseParent?.(key.upArrow?-1:-10);
      else if(key.downArrow||key.pageDown||key.wheelDown)props.onBrowseParent?.(key.downArrow?1:10);
      return;
    }
    if(key.pageUp||key.pageDown||key.wheelUp||key.wheelDown){
      scrollDiscussion((key.pageUp||key.wheelUp?-1:1)*(key.pageUp||key.pageDown?10:3));return;
    }
    if(focus!=="choices"&&(key.upArrow||key.downArrow)){
      scrollDiscussion(key.upArrow?-1:1);return;
    }
    if(focus === "body"){
      if(key.return){setFocus("input");return;}
      if(input&&!key.ctrl&&!key.meta){setFocus("input");draft.insertText(input);}
      return;
    }
    if (focus === "choices") {
      if (key.leftArrow) {
        setSelection((value) => (value + choices.length - 1) % choices.length);
      } else if (key.rightArrow) {
        setSelection((value) => (value + 1) % choices.length);
      } else if (key.upArrow) {
        setFocus("input");
      } else if (key.return && !busy) {
        finishChoice();
      }
      return;
    }

    if (key.leftArrow) { draft.moveCursor(-1); return; }
    if (key.rightArrow) { draft.moveCursor(1); return; }
    if (key.home) { draft.moveToStart(); return; }
    if (key.end) { draft.moveToEnd(); return; }
    if (key.backspace) { draft.deleteBack(); return; }
    if (key.delete) { draft.deleteForward(); return; }
    if (key.return) {
      if (draft.value.trim() && !busy) {
        const question = draft.value.trim();
        draft.setValue("");
        ask(question);
      } else if (!draft.value.trim()) {
        setFocus("choices");
      }
      return;
    }
    if (!key.ctrl && !key.meta && input){setFocus("input");draft.insertText(input);}
  });

  const visibleAnswer = stream.text || answer;
  const visibleReasoning = stream.reasoning || reasoning;
  const latestTool = stream.tools[stream.tools.length - 1];

  useEffect(()=>{
    const timer=setTimeout(()=>{
      if(content.current)setBodyRows(Math.max(2,Math.min(maximum,measureElement(content.current).height)));
    },0);
    return()=>clearTimeout(timer);
  },[visibleAnswer,visibleReasoning,props.cols,maximum,stream.tools.length,error]);

  return (
    <PageFrame page={pages.discussion}>
      <Text bold color={colors.accent}>{t({en:"BTW · isolated, read-only",zh:"BTW · 独立只读讨论"})}</Text>
      {confirm?<Text color={colors.warning}>{t({en:"Confirm target: ",zh:"确认目标："})}{targetName} · {selection===3?t({en:"interrupt and append",zh:"中断并追加"}):t({en:"append at boundary",zh:"边界追加"})} · {t({en:"←/→ target · Enter confirm · Esc cancel",zh:"←/→ 目标 · Enter 确认 · Esc 取消"})}</Text>:null}
      <ScrollBox ref={body} flexDirection="column" height={bodyRows} flexGrow={0} flexShrink={1} minHeight={1}>
      <Box ref={content} flexDirection="column" flexShrink={0} width={width}>
      {latestQuestion?<Text dimColor>{t({en:"Question: ",zh:"问题："})}{latestQuestion}</Text>:null}
      {visibleReasoning && props.verbose ? <Text dimColor>{visibleReasoning}</Text> : null}
      {visibleAnswer ? <MarkdownText content={visibleAnswer} width={width}/> : (
        <Text dimColor>{busy
          ? t({en:"Working…",zh:"正在处理…"})+(latestTool ? " · "+latestTool.tool_name : "")
          : t({en:"No answer",zh:"尚无回答"})}</Text>
      )}
      {latestTool && !props.verbose ? (
        <Text dimColor>
          {latestTool.is_running ? t({en:"Using",zh:"正在调用"}) : latestTool.allowed ? t({en:"Used",zh:"已调用"}) : t({en:"Tool failed",zh:"工具调用失败"})} · {latestTool.tool_name}
        </Text>
      ) : null}
      {props.verbose && stream.tools.length > 0 ? (
        <Box flexDirection="column">
          {stream.tools.map((tool, index) => <ToolCallDisplay
            key={tool.tool_call_id || index} tool={tool}
            width={width} />)}
        </Box>
      ) : null}
      {error ? <Text color={colors.danger}>{error}</Text> : null}

      </Box></ScrollBox>
      <Box flexShrink={0} flexDirection="row">
        <Text color={focus === "input" ? colors.accent : undefined}>▸ </Text>
        <TextInput
          value={draft.value}
          cursorOffset={draft.cursor}
          placeholder={busy ? t({en:"Working…",zh:"正在处理…"}) : t({en:"Follow-up",zh:"继续提问"})}
          focus={focus === "input"}
          showCursor={focus === "input" && !busy}
          maxWidth={width}
        />
      </Box>
      <Box flexShrink={0} flexDirection="row" flexWrap="wrap" gap={1}>
        {choices.map((choice, index) => {
          const active = focus === "choices" && index === selection;
          return (
            <Text key={choice} bold={active}
              color={active ? colors.selection.block.fg : undefined}
              backgroundColor={active ? colors.selection.block.bg : undefined}
              dimColor={!active}>{` ${choice} `}</Text>
          );
        })}
      </Box>
      <Text dimColor>{focus==="parent"?t({en:"Parent conversation · ↑/↓ or PgUp/PgDn browse · Tab discussion · Esc close",zh:"主/子会话 · ↑/↓ 或 PgUp/PgDn 浏览 · Tab 返回讨论 · Esc 关闭"}):
        focus==="body"?t({en:"↑/↓ scroll · type to reply · Tab switch · Esc close",zh:"↑/↓ 滚动 · 直接输入追问 · Tab 切换 · Esc 关闭"}):
        focus==="input"?t({en:"Enter ask · ↑/↓ scroll · Tab actions · Esc close",zh:"Enter 提问 · ↑/↓ 滚动 · Tab 操作 · Esc 关闭"}):
        t({en:"←/→ actions · Enter apply · Tab parent · Esc close",zh:"←/→ 操作 · Enter 执行 · Tab 主会话 · Esc 关闭"})}</Text>
    </PageFrame>
  );
}
