// src/daemon/streamReducer.ts — pure reducer for the native daemon token stream.
// Extracted from useChat's inline onChunk so the event→(text + tool cards)
// translation is testable and free of React/transport concerns. `toolIndex`
// maps tool_call_id and tool_name to an index in `tools` (two keys, one entry).
import type { LiveStageDetail, ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import type { StreamEvent } from "./streamEvents.js";
import { runtimeNoticeFromEvent } from "./runtimeNotices.js";
import {updatePublicProgress, progressIdentity, isRuntimePreparation} from "../chat/publicProgress.js";
import {withRequestContext} from "../chat/requestInputs.js";

export type StreamState = {
  stage?:LiveStageDetail;
  stageSequence:number;
  text: string;
  reasoning: string;
  thinking: boolean;
  thinkingText: string;
  thinkingStartedAt?:string;
  tools: ToolCallCard[];
  toolIndex: Map<string, number>;
  publicProgressIndex: Map<string, number>;
  notices: RuntimeNotice[];
  activity: StreamActivity[];
  noticeKeys: Set<string>;
  visibility: "public" | "verbose";
  providerStep: number;
  providerUsage?: ProviderUsageSummary;
};

export function initStreamState(): StreamState {
  return {
    stageSequence:0,
    text: "", reasoning: "", thinking: false, thinkingText: "", tools: [], toolIndex: new Map(), publicProgressIndex: new Map(),
    notices: [], activity: [], noticeKeys: new Set(), visibility: "verbose", providerStep: 0,
  };
}

type ReduceOptions = {
  /** Child events projected into A must not commit or clear A's response draft. */
  preservePendingText?: boolean;
};

const THINKING_ENDED_BY = new Set([
  "text_delta", "progress_delta", "progress_summary", "provider_response_completed",
  "toolcall_start", "toolcall_delta", "toolcall_done", "tool_result", "tool_progress",
  "composite_step_result", "decision_required", "agent_terminal", "agent_complete", "stream_recovery",
]);

function eventActor(ev: StreamEvent): string | undefined {
  const actor = (ev as unknown as Record<string, unknown>).actor_label;
  return typeof actor === "string" && actor ? actor : undefined;
}

function toolKey(ev: StreamEvent, value: string): string {
  return `${String(ev.process_id || "host")}:${value}`;
}

function findToolIndex(state: StreamState, ev: StreamEvent): number | undefined {
  const id = "tool_call_id" in ev ? String(ev.tool_call_id || "") : "";
  const name = "tool_name" in ev ? String(ev.tool_name || "") : "";
  return (id ? state.toolIndex.get(toolKey(ev, id)) : undefined)
    ?? (name ? state.toolIndex.get(toolKey(ev, name)) : undefined);
}

/** Preserve the producer's visibility when a draft becomes a completed step. */
function commitProgress(state: StreamState): StreamState {
  const text = state.text.trim();
  if (!text) return {...state, text: ""};
  const hasDraft = state.activity.some(item => item.kind === "draft");
  return {...state, text: "", activity: hasDraft
    ? state.activity.map(item => item.kind === "draft" ? {...item,kind: "progress" as const} : item)
    : [...state.activity,{kind: "progress",text,visibility: state.visibility}]};
}

function appendNotice(state: StreamState, ev: StreamEvent): StreamState {
  const notice = runtimeNoticeFromEvent(ev);
  if (!notice || state.noticeKeys.has(notice.key)) return state;
  const noticeKeys = new Set(state.noticeKeys);
  noticeKeys.add(notice.key);
  const notices = [...state.notices, notice];
  return {
    ...state, notices, noticeKeys,
    activity: [...state.activity, {
      kind: "notice", noticeIndex: notices.length - 1, detail_ref: notice.detail_ref,
    }],
  };
}

export function reduceStreamEvent(
  state: StreamState,
  ev: StreamEvent,
  options: ReduceOptions = {},
): StreamState {
  if(isRuntimePreparation(ev))return state;
  // A cycle owns its model output and following tools. A fresh request,
  // pause, terminal event or recovery retires its transient detail. This is
  // observed causal grouping, not semantic matching of adjacent prose.
  if(!options.preservePendingText){
    if(ev.event==='provider_request_started' && ev.phase!=='alignment'){
      const sequence=state.stageSequence+1;
      state={...state,stageSequence:sequence,stage:{id:`${ev.process_id||''}:${sequence}`,process_id:ev.process_id||'',reasoning:''}};
    }else if(ev.event==='reasoning_delta'){
      const stage=state.stage?.process_id===(ev.process_id||'')?state.stage:undefined;
      const sequence=stage?state.stageSequence:state.stageSequence+1;
      state={...state,stageSequence:sequence,stage:{id:stage?.id||`${ev.process_id||''}:${sequence}`,
        process_id:ev.process_id||'',reasoning:(stage?.reasoning||'')+(ev.delta||'')}};
    }else if(['decision_required','agent_terminal','agent_complete','stream_recovery'].includes(ev.event)){
      state={...state,stage:undefined};
    }
  }
  const observedAt="time" in ev && typeof ev.time==="string" ? ev.time : undefined;
  // This is transient presentation state. Keep the original reasoning evidence,
  // but never recycle it as a public stage explanation or revive an old thought.
  // Projected child activity cannot change the main process's thinking phase.
  if (!options.preservePendingText) {
    if (ev.event === "provider_request_started" && ev.phase !== "alignment") {
      state = {...state, thinking: true, thinkingText: "",thinkingStartedAt:observedAt};
    } else if (ev.event === "reasoning_delta") {
      state = {...state, thinking: true, thinkingText: (state.thinking ? state.thinkingText : "") + (ev.delta || ""),
        thinkingStartedAt:state.thinking ? state.thinkingStartedAt : observedAt};
    } else if (state.thinking && THINKING_ENDED_BY.has(ev.event)) {
      state = {...state, thinking: false, thinkingText: "",thinkingStartedAt:undefined};
    }
  }
  switch (ev.event) {
    case "text_delta": {
      const visibility = ev.visibility === "public" ? "public" : ev.visibility === "verbose" ? "verbose" : state.visibility;
      // A channel change ends the old draft. The active response text must
      // never inherit private fragments and merely adopt the latest label.
      const current = visibility === state.visibility ? state : commitProgress(state);
      const activity=current.activity.slice(), lastIndex=activity.findLastIndex(item=>item.kind!=="context"),last=activity[lastIndex];
      const actor=eventActor(ev), delta=ev.delta || "";
      if(last?.kind === "draft" && last.process_id === ev.process_id && last.actor_label === actor
          && last.visibility === visibility) {
        activity[lastIndex]={...last,text:last.text+delta};
      } else if(delta) activity.push({kind:"draft",text:delta,visibility,process_id:ev.process_id,actor_label:actor});
      return {...current,activity,text:current.text+delta,visibility,providerStep:ev.step ?? state.providerStep};
    }

    case "reasoning_delta":
      {
        const delta = ev.delta || "";
        const activity = state.activity.slice();
        const last = activity[activity.length - 1];
        const actor = eventActor(ev);
        if (last?.kind === "reasoning"
            && last.process_id === ev.process_id && last.actor_label === actor) {
          activity[activity.length - 1] = {...last, text: last.text + delta};
        } else {
          activity.push({kind: "reasoning", text: delta, process_id: ev.process_id, actor_label: actor});
        }
        return {
          ...state,
          reasoning: state.reasoning + delta,
          activity,
          providerStep: ev.step ?? state.providerStep,
        };
      }

    case "progress_delta":
    case "progress_summary": {
      const publicProgressIndex = ev.update_id && !state.publicProgressIndex.has(progressIdentity(ev))
        ? new Map(state.publicProgressIndex) : state.publicProgressIndex;
      return {...state, publicProgressIndex,
        activity: updatePublicProgress(state.activity, {...ev, actor_label: eventActor(ev),
          stage_id:state.stage?.process_id===(ev.process_id||'')?state.stage.id:undefined}, publicProgressIndex)};
    }

    case "provider_usage": {
      const raw = (ev as unknown as {usage?: Record<string, unknown>}).usage || {};
      const next = {
        input_tokens: Number(raw.input_tokens || 0),
        output_tokens: Number(raw.output_tokens || 0),
      };
      return {
        ...state,
        providerUsage: {
          input_tokens: (state.providerUsage?.input_tokens || 0) + next.input_tokens,
          output_tokens: (state.providerUsage?.output_tokens || 0) + next.output_tokens,
        },
      };
    }

    case "provider_request_started":
      // Show the current provider turn. Previous turns remain in the durable
      // trace instead of being concatenated into one repeated chat response.
      {
        // Semantic review emits provider telemetry, never public answer text.
        // A typed text continuation appends to the same public answer draft.
        if (ev.phase === "alignment") return appendNotice(state, ev);
        const preserveText = options.preservePendingText || ev.answer_continuation === true;
        const committed = preserveText ? state : commitProgress(state);
        const noticed = appendNotice(committed, ev);
        return {
        ...noticed,
        activity:withRequestContext(noticed.activity,ev,eventActor(ev)),
        text: preserveText ? state.text : "",
        visibility: ev.task_kind === "answer" ? "public" : "verbose",
        providerStep: ev.step ?? (state.providerStep + 1),
        };
      }

    case "provider_request_parameters":
      return ev.purpose==="alignment" ? state : {...state,activity:withRequestContext(state.activity,ev,eventActor(ev))};

    case "toolcall_start": {
      const committed = options.preservePendingText ? state : commitProgress(state);
      const idx = committed.tools.length;
      const callId = ev.tool_call_id || ev.tool_name || "";
      const toolIndex = new Map(committed.toolIndex);
      if (callId) toolIndex.set(toolKey(ev, callId), idx);
      if (ev.tool_name) toolIndex.set(toolKey(ev, ev.tool_name), idx);
      const card: ToolCallCard = {
        tool_name: ev.tool_name || "",
        tool_call_id: callId,
        target: ev.target || "",
        allowed: true,
        status_label: "OK",
        duration_ms: 0,
        timestamp: ev.time || "",
        is_running: true,
        state: "running",
        actor_label: eventActor(ev),
        process_id: ev.process_id,
        detail_ref: ev.detail_ref,
      };
      return {
        ...committed, tools: [...committed.tools, card], toolIndex,
        activity: [...committed.activity, {kind: "tool", toolIndex: idx, detail_ref: ev.detail_ref}],
      };
    }

    case "toolcall_done": {
      const idx=findToolIndex(state,ev);
      if(idx===undefined)return state;
      const tools=state.tools.slice();
      tools[idx]={...tools[idx],argument_preview:ev.argument_preview as Record<string,unknown>|undefined,
        detail_ref:typeof ev.detail_ref==="string"?ev.detail_ref:tools[idx].detail_ref};
      return {...state,tools};
    }
    case "tool_context_prepared": {
      const idx=findToolIndex(state,ev);
      if(idx===undefined)return state;
      const tools=state.tools.slice();
      tools[idx]={...tools[idx],context_only:true,is_running:false,state:"pending"};
      return {...state,tools};
    }
    case "toolcall_delta": {
      const idx = findToolIndex(state, ev);
      if (idx === undefined) return state;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        target: ((tools[idx].target || "") + (ev.delta || "")).slice(0, 500),
      };
      return { ...state, tools };
    }

    case "tool_result": {
      const existing = findToolIndex(state, ev);
      if (existing === undefined) {
        const idx = state.tools.length;
        const callId = ev.tool_call_id || ev.tool_name || "";
        const toolIndex = new Map(state.toolIndex);
        if (callId) toolIndex.set(toolKey(ev, callId), idx);
        if (ev.tool_name) toolIndex.set(toolKey(ev, ev.tool_name), idx);
        const card: ToolCallCard = {
          tool_name: ev.tool_name,
          tool_call_id: callId,
          target: "",
          result_text: ev.result_preview || ev.error || "",
          compact_summary: ev.compact_summary,
          diff: ev.diff,
          blocked_reason: ev.is_error ? (ev.error || "tool failed") : undefined,
          duration_ms: ev.duration_ms || 0,
          allowed: !ev.is_error,
          status_label: ev.is_error ? "ERROR" : "OK",
          timestamp: ev.time || "",
          is_running: false,
          state: ev.is_error ? "error" : "completed",
          actor_label: eventActor(ev),
          process_id: ev.process_id,
          detail_ref: ev.detail_ref,
        };
        return {
          ...state, tools: [...state.tools, card], toolIndex,
          activity: [...state.activity, {kind: "tool", toolIndex: idx, detail_ref: ev.detail_ref}],
        };
      }
      const idx = existing;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        result_text: ev.result_preview || ev.error || "",
        compact_summary: ev.compact_summary || tools[idx].compact_summary,
        diff: ev.diff || tools[idx].diff,
        blocked_reason: ev.is_error ? (ev.error || "tool failed") : undefined,
        duration_ms: ev.duration_ms || 0,
        allowed: !ev.is_error,
        is_running: false,
        state: ev.is_error ? "error" : "completed",
        detail_ref: ev.detail_ref || tools[idx].detail_ref,
      };
      return { ...state, tools };
    }

    case "composite_step_result": {
      const rawKey = `${ev.execution_id}:${ev.step_id}`;
      const key = toolKey(ev, rawKey);
      if (state.toolIndex.has(key)) return state;
      const idx = state.tools.length;
      const toolIndex = new Map(state.toolIndex);
      toolIndex.set(key, idx);
      const card: ToolCallCard = {
        tool_name: ev.tool_name,
        tool_call_id: rawKey,
        composite_tool: ev.composite_tool, execution_id: ev.execution_id, step_id: ev.step_id,
        target: `${ev.composite_tool} / ${ev.step_id}`,
        result_text: ev.error || `receipt ${ev.receipt?.receipt_id || "recorded"}`,
        blocked_reason: ev.is_error ? (ev.error || "composite step failed") : undefined,
        duration_ms: ev.duration_ms || 0,
        allowed: !ev.is_error,
        status_label: ev.is_error ? "ERROR" : "OK",
        timestamp: ev.time || "",
        is_running: false,
        state: ev.is_error ? "error" : "completed",
        actor_label: eventActor(ev),
        process_id: ev.process_id,
      };
      return {
        ...state, tools: [...state.tools, card], toolIndex,
        activity: [...state.activity, {kind: "tool", toolIndex: idx}],
      };
    }

    case "tool_progress": {
      const idx = findToolIndex(state, ev);
      if (idx === undefined) return state;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        status_label: ev.status || tools[idx].status_label,
        allowed: ev.status !== "blocked",
        blocked_reason: ev.status === "blocked" ? ev.reason : undefined,
        is_running: ev.status === "running",
        state:
          ev.status === "blocked"
            ? "error"
            : ev.status === "running"
              ? "running"
              : "completed",
      };
      return { ...state, tools };
    }

    case "stream_recovery": {
      const tools = state.tools.map((t) =>
        t.is_running ? {
          ...t, is_running: false, state: "error" as const, allowed: false,
          blocked_reason: "Stream interrupted before a durable tool result was received",
        } : t,
      );
      return appendNotice({ ...state, tools }, ev);
    }

    default:
      return appendNotice(state, ev);
  }
}

export function finalizeTools(tools: ToolCallCard[]): ToolCallCard[] {
  return tools.map((t) => ({
    ...t,
    is_running: false,
    state: !t.state || t.state === "running" ? "unresolved" : t.state,
    allowed: !t.state || t.state === "running" ? false : t.allowed,
    blocked_reason: !t.state || t.state === "running"
      ? (t.blocked_reason || "Tool result unavailable when the turn ended")
      : t.blocked_reason,
  }));
}
