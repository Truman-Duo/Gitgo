import {SelectionText} from "./Selection.js";
import {label} from "../ui/runtimeLabels.js";
import React, {useEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import {taskMechanism} from "../backend/tools.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {colors, truncate, usePanelSize} from "../theme/index.js";
import {contextSessions} from "./RuntimeContextPanel.js";
import {InlineChoiceStrip} from "./InlineChoiceStrip.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {cycleChoiceIndex} from "../input/choice.js";

type Props = {client: BackendClient; project: string; processes: Record<string, ProcessInfo>;
  onDismiss: () => void; interactive?: boolean};

export function TaskMechanismsPanel({client, project, processes, onDismiss, interactive = true}: Props) {
  const t = useCopy();
  const {w} = usePanelSize({minWidth: 20, widthOffset: 6});
  const rows = contextSessions(Object.values(processes)).filter(p => p.task_maintenance?.revision != null || p.harness_mechanisms?.length);
  const [selectedId, setSelectedId] = useState("");
  const index = Math.max(0, rows.findIndex(p => p.process_id === selectedId));
  const selected = rows[index];
  const currentId = useRef(selected?.process_id);
  currentId.current = selected?.process_id;
  const [detail, setDetail] = useState(false), [data, setData] = useState<any>(null);
  const feedback = useFeedback({kind:"page",id:`mechanisms:${project}:${selected?.process_id || ""}`},{tone:"error"});
  const setError = feedback.show;
  const [busy, setBusy] = useState(false);
  const [actionIndex, setActionIndex] = useState(0), [confirm, setConfirm] = useState(false), [refresh, setRefresh] = useState(0);
  const scroll = useRef<ScrollBoxHandle>(null), mutation = useRef(false);
  const alive = useRef(true);
  useEffect(() => () => {alive.current = false;}, []);
  const epoch = selected?.task_maintenance?.epoch;
  const extensionStamp = JSON.stringify(selected?.harness_mechanisms || []);
  useEffect(() => {
    let current = true;
    setConfirm(false);
    if (!selected) {setData(null); return;}
    void taskMechanism(client, project, selected.process_id).then(value => {if (current) setData(value);})
      .catch(reason => {if (current) setError(String(reason?.message || reason));});
    return () => {current = false;};
  }, [client, project, selected?.process_id, epoch, extensionStamp, refresh]);
  useEffect(() => {setData(null); setDetail(false); scroll.current?.scrollTo(0);}, [selected?.process_id]);
  useScrollInput(scroll, interactive && detail && !confirm);

  const state = data?.state;
  const hasTaskMechanism = Number.isInteger(state?.revision) && state.revision > 0;
  const live = selected && ["running", "waiting", "awaiting_user"].includes(selected.status);
  const actions: {id:string;label:string;instance?:any}[] = live && hasTaskMechanism && !state?.detached ? [{id: state?.suspended ? "resume" : "suspend", label: state?.suspended
    ? t({en:"Resume",zh:"恢复"}) : t({en:"Suspend",zh:"暂停"})}, {id:"detach", label:t({en:"Detach",zh:"卸载"})}]
    : [];
  for (const item of data?.extensions || []) {
    if (item.phase === "blocked" && item.error?.effect_state === "unknown") actions.push({id:`reconcile:${item.id}`,
      label:t({en:"Reconcile",zh:"核对恢复"}) + ` · ${item.id}`, instance:item});
  }
  const action = actions[Math.min(actionIndex, Math.max(0, actions.length - 1))];
  const change = () => {
    if (mutation.current || !action || !selected) return;
    if (!confirm) {setConfirm(true); return;}
    mutation.current = true; setBusy(true); setError("");
    void taskMechanism(client, project, selected.process_id, action.instance
      ? {action:"reconcile",instance_id:action.instance.id,task_id:selected.active_task_id,expected_revision:action.instance.revision}
      : {action:action.id,task_id:state.task_id,expected_revision:state.revision})
      .then(value => {if (alive.current && currentId.current === selected.process_id) {
        if (action.instance) {setRefresh(n=>n+1); if (!value.reconciled) setError(value.reason || t({en:"Evidence is still unavailable; the instance remains blocked.",zh:"证据仍不充分，实例继续阻塞。"}));}
        else setData(value);
      }})
      .catch(reason => {if (alive.current) setError(String(reason?.message || reason));})
      .finally(() => {mutation.current = false; if (alive.current) {setBusy(false); setConfirm(false);}});
  };
  useInput((input, key) => {
    if (matchChord("escape", input, key)) {
      if (busy) return;
      if (confirm) setConfirm(false);
      else if (detail) {setDetail(false); scroll.current?.scrollTo(0);}
      else onDismiss();
      return;
    }
    if (busy) return;
    if (detail) {
      if (confirm) {if (matchChord("enter", input, key)) change(); return;}
      if (matchChord("left", input, key)) setActionIndex(cycleChoiceIndex(actionIndex,-1,actions.length));
      else if (matchChord("right", input, key)) setActionIndex(cycleChoiceIndex(actionIndex,1,actions.length));
      else if (matchChord("enter", input, key)) change();
      return;
    }
    const direction = matchChord("up", input, key) ? -1 : matchChord("down", input, key) ? 1 : 0;
    if (direction) {
      const next = Math.max(0, Math.min(rows.length - 1, index + direction));
      setSelectedId(rows[next]?.process_id || "");
      scroll.current?.scrollTo(Math.max(0, next));
    } else if (matchChord("enter", input, key) && selected) {setDetail(true); scroll.current?.scrollTo(0);}
    else if (input === "r") setRefresh(v => v + 1);
  }, {isActive: interactive});
  const phase = state?.suspended ? t({en:"Suspended",zh:"已暂停"}) : state?.phase === "ready"
    ? t({en:"Ready",zh:"就绪"}) : state?.phase === "needs_review" ? t({en:"Needs review",zh:"待修订"})
    : t({en:"Current",zh:"当前"});
  return <Box flexDirection="column" paddingX={1} paddingTop={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>{t({en:"Task mechanisms —",zh:"任务机制 —"})} {project}</Text>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
      {!rows.length ? <Text dimColor>{t({en:"No task mechanism has been activated.",zh:"尚无已启用的任务机制。"})}</Text>
        : !detail ? rows.map((p, i) => <SelectionText selected={i === index} focused={interactive} key={p.process_id}
             >
            {truncate((p.display_name || (p.parent_id ? t({en:"Subprocess",zh:"子进程"}) : t({en:"Main process",zh:"主进程"}))) + " · " +
              (p.task_maintenance?.phase === "needs_review" ? t({en:"Needs review",zh:"待修订"}) : t({en:"Current",zh:"当前"})), w - 4)}
          </SelectionText>) : !data ? <Text dimColor>{t({en:"Loading…",zh:"正在读取…"})}</Text> : <>
          <Text bold>{data.contract?.goal || state?.definition?.intent}</Text>
          <Text>{t({en:"State: ",zh:"状态："})}{phase} · {String(data.advancement?.mode || "").toUpperCase()}</Text>
          <Text dimColor>{t({en:"Contract / plan revisions: ",zh:"契约 / 计划版本："})}{data.contract?.revision ?? 0} / {state?.plan?.revision ?? 0}</Text>
          <Text dimColor>{t({en:"Consumers: ",zh:"消费者："})}{data.consumer_projection?.epoch === state?.epoch
            ? t({en:"Synchronized",zh:"已同步"}) : t({en:"Refresh at the next execution boundary",zh:"在下一执行边界同步"})}</Text>
          <Text>{state?.plan?.summary}</Text>
          {(data.extensions || []).length ? <Text bold>{t({en:"Harness extensions",zh:"Harness 扩展"})}</Text> : null}
          {(data.extensions || []).map((item:any) => <Box key={item.id} flexDirection="column" marginTop={1}>
            <Text>{item.id} · {label(item.phase,t)} · {t({en:"Generation",zh:"代际"})} {item.epoch}</Text>
            <Text dimColor>{t({en:"Definition / instance revisions: ",zh:"定义 / 实例版本："})}{item.definition_revision} / {item.revision}</Text>
            <Text dimColor>{item.version_policy === "pinned" ? t({en:"Pinned version",zh:"固定版本"}) : t({en:"Follow compatible updates",zh:"跟随兼容更新"})}</Text>
            {item.error ? <Text color={colors.danger}>{item.error.code} · {label(item.error.effect_state,t)}</Text> : null}
            {(item.maintenance || []).map((event:any) => <Text dimColor key={event.id}>{label(event.status,t)}</Text>)}
            {Object.keys(item.completion?.return_counters || {}).length ? <Text dimColor>
              {t({en:"Returns to execution: ",zh:"返回执行次数："})}{Object.values(item.completion.return_counters as Record<string,number>).reduce((sum,value)=>sum+value,0)}
            </Text> : null}
            {(item.completion?.reviews || []).map((review:any) => <Text key={review.reviewer_process_id}>
              {t({en:"Reviewer",zh:"审阅进程"})} {review.reviewer_process_id.slice(0,8)} · {label(review.phase,t)} · {t({en:"Generation",zh:"代际"})} {review.epoch}
              {review.verdict ? ` · ${label(review.verdict,t)}` : ""}
            </Text>)}
            {(item.inbox || []).map((candidate:any) => <Box key={candidate.id} flexDirection="column" marginTop={1}>
              <Text>{label(candidate.status,t)} · {candidate.text}</Text>
              {candidate.processing_scope ? <Text dimColor>{t({en:"Reconciled with the original task plan; execution is tracked by that task.",zh:"已与原任务计划对齐；执行结果仍由原任务跟踪。"})}</Text> : null}
              {candidate.adoption_error ? <Text color={colors.danger}>{candidate.adoption_error}</Text> : null}
            </Box>)}
          </Box>)}
          {(state?.plan?.steps || []).map((step: any) => <Box key={step.id} flexDirection="column" marginTop={1}>
            <Text>{step.goal}</Text><Text dimColor>{t({en:"Verify: ",zh:"验证："})}{step.verification}</Text>
          </Box>)}
          <Text bold>{t({en:"Adopted requests and maintenance",zh:"增量需求与维护"})}</Text>
          {Object.values(state?.changes || {}).map((item: any) => <Box key={item.id} flexDirection="column" marginTop={1}>
            <Text color={item.status === "pending" ? colors.warning : undefined}>{label(item.status,t)}</Text>
            <Text>{item.content}</Text>
            {item.resolution?.reason ? <Text dimColor>{item.resolution.reason}</Text> : null}
          </Box>)}
          {Object.values(state?.maintenance_items || {}).map((item: any) => <Text key={item.id}>
            {label(item.status,t)} · {item.source}
          </Text>)}
          <Text bold>{t({en:"Necessary related work",zh:"必要关联工作"})}</Text>
          <Text>{state?.related?.summary}</Text>
          {Object.values(state?.related?.candidates || {}).map((item: any) => <Box key={item.id} flexDirection="column" marginTop={1}>
            <Text>{item.work} · {label(item.status,t)}</Text>
            <Text dimColor>{item.reason}</Text><Text dimColor>{item.owner} · {item.verification}</Text>
          </Box>)}
          {state?.detached ? <Text dimColor>{t({en:"Detached; accepted obligations and evidence are retained.",zh:"已卸载；已接受的义务与证据仍保留。"})}</Text> : null}
        </>}
    </ScrollBox>
    <FeedbackMessage feedback={feedback.message}/>
    {detail && actions.length ? <InlineChoiceStrip items={actions} selected={Math.min(actionIndex,actions.length-1)} width={w} interactive={interactive && !busy}/> : null}
    <Text dimColor>{busy ? t({en:"Saving…",zh:"正在保存…"}) : confirm
      ? t({en:"Enter confirm · Esc cancel · accepted obligations remain",zh:"Enter 确认 · Esc 取消 · 已接受的义务仍保留",keys:true})
      : detail ? actions.length ? t({en:"↑/↓ scroll · ←/→ action · Enter select · Esc list",zh:"↑/↓ 滚动 · ←/→ 动作 · Enter 选择 · Esc 列表",keys:true})
      : t({en:"↑/↓ scroll · Esc list · Adjust extensions in the conversation",zh:"↑/↓ 滚动 · Esc 列表 · 可在对话中调整扩展",keys:true})
      : t({en:"↑/↓ select · Enter detail · R refresh · Esc tabs",zh:"↑/↓ 选择 · Enter 详情 · R 刷新 · Esc 页签",keys:true})}</Text>
  </Box>;
}
