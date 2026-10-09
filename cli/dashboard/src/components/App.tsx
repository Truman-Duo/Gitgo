// src/components/App.tsx — Three-scene routing with createStore
import React, { useCallback, useMemo, useState, useRef, useEffect } from "react";
import { Box, Text, useApp, useTerminalSize } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import {NativeHostClient, type BackendClient} from "../backend/client.js";
import { colors, contextPct } from "../theme/index.js";
import { useGitgoData } from "../hooks/useGitgoData.js";
import { useLoopData, type ProcessInfo } from "../hooks/useLoopData.js";
import { useLLMConfig } from "../hooks/useLLMConfig.js";
import { createReducerStore, reducer, useStore, initialAppState, type AppState, type AppAction, type OverlayType, type Scene } from "../state/store.js";
import { getCommands, type CommandContext } from "../commands.js";
import { resolveSceneKey, type InputContext, type InputAction, type TextBuffer, type TextOp } from "../input/keymap.js";
import type { ChatScrollHandle, PendingDecision } from "../types.js";
import { runCommandEffect, stopProcessEffect, type RunCommandDeps, type StopProcessDeps } from "../effects/run.js";
import { noticeToActions } from "../notices.js";
import { useTextInput, type UseTextInputReturn, applyTextOp as applyTextOpToBuf } from "../hooks/useTextInput.js";
import { Overview } from "./Overview.js";
import { ProjectWorkspace } from "./ProjectWorkspace.js";
import { ProcessList, visibleBProcesses } from "./ProcessList.js";
import { AgentDetail, AgentDetailScene } from "./AgentDetail.js";
import { CommandBar, type FooterConfig } from "./CommandBar.js";
import { renderOverlay } from "./overlays.js";
import { ScrollableInputLayer } from "../input/runtime.js";
import { chordLabel } from "../input/bindings.js";
import { notifyGeneralConfigChanged, useGeneralConfig } from "../hooks/useGeneralConfig.js";
import { runningWorkers } from "../daemon/agentLabels.js";
import { editPromptExternally } from "../utils/externalPromptEditor.js";

export type StartupSmokeTask = {
  project: string;
  message: string;
  manualDelegation?: boolean;
  autoAllowOnce?: boolean;
};

function processRuntimeStatus(process: ProcessInfo | undefined): string {
  if (!process) return "";
  const parts: string[] = [];
  if (process.context) parts.push(`epoch ${process.context.epoch}`);
  if (process.worktree) parts.push(process.worktree.isolated ? "wt isolated" : "wt shared");
  return parts.filter(Boolean).join(" · ");
}

type Props = {
  client: BackendClient;
  refreshSec?: number;
  startupSmokeTask?: StartupSmokeTask;
  startupTerminalSetup?: boolean;
  startupNotice?: string;
  onTerminalSetupComplete?: () => Promise<{completed: boolean; message: string}>;
};

const appStore = createReducerStore<AppState, AppAction>(reducer, initialAppState());

export function App({ client, refreshSec = 5, startupSmokeTask, startupTerminalSetup = false,
  startupNotice = "", onTerminalSetupComplete }: Props) {
  const { exit } = useApp();
  const { columns: termCols, rows: termRows } = useTerminalSize();
  const { projects, loading, error, refresh } = useGitgoData(client, refreshSec);
  const { providers: globalProviders, fetchStatus: fetchGlobalLLM } = useLLMConfig(client);
  const generalConfig = useGeneralConfig(client);

  const scene = useStore(appStore, (s) => s.scene);
  const activeProject = useStore(appStore, (s) => s.activeProject);
  const loopData = useLoopData(client, activeProject, 2); // single runtime poll source for the active project
  const { providers, processes } = loopData;
  const activeAgentId = useStore(appStore, (s) => s.activeAgentId);
  const processListSelIdx = useStore(appStore, (s) => s.processListSelIdx);
  const runningBSelIdx = useStore(appStore, (s) => s.runningBSelIdx);
  const sel = useStore(appStore, (s) => s.sel);
  const mode = useStore(appStore, (s) => s.mode);
  const cmdResult = useStore(appStore, (s) => s.cmdResult);
  const overlayStack = useStore(appStore, (s) => s.overlayStack);
  const suggestionIdx = useStore(appStore, (s) => s.suggestionIdx);
  const refreshKey = useStore(appStore, (s) => s.refreshKey);
  const chatInputFocused = useStore(appStore, (s) => s.chatInputFocused);
  const statusBarFocused = useStore(appStore, (s) => s.statusBarFocused);
  const decisionSelection = useStore(appStore, (s) => s.decisionSelection);
  const decisionComposing = useStore(appStore, (s) => s.decisionComposing);
  const decisionSubmitting = useStore(appStore, (s) => s.decisionSubmitting);

  const dispatch = appStore.dispatch;

  // ── Text input hooks ─────────────────────────────────────
  const textInput = useTextInput("");
  const cmdInput = useTextInput("");
  const llmCmdInput = useTextInput("");

  // Auto-defocus: when text becomes empty, return focus to list
  useEffect(() => {
    if (textInput.value === "" && chatInputFocused) {
      dispatch({ type: "set_chat_input_focused", focused: false });
    }
  }, [textInput.value, chatInputFocused, dispatch]);

  // Command feedback auto-dismiss: transient result text fades after 4s.
  useEffect(() => {
    if (!cmdResult) return;
    const t = setTimeout(() => dispatch({ type: "set_cmd_result", text: "" }), 4000);
    return () => clearTimeout(t);
  }, [cmdResult, dispatch]);

  // ── Overlay stack helpers ────────────────────────────────
  const pushOverlay = useCallback((type: OverlayType, props?: Record<string, any>) => {
    cmdInput.setValue("");
    dispatch({ type: "push_overlay", overlay: type, props });
  }, [dispatch, cmdInput]);

  const popOverlay = useCallback(() => {
    llmCmdInput.setValue("");
    setFooterOverride(null);
    dispatch({ type: "pop_overlay" });
  }, [dispatch, llmCmdInput]);

  const [terminalSetupNeeded, setTerminalSetupNeeded] = useState(startupTerminalSetup);
  const terminalSetupShown = useRef(false);
  const terminalSetupObserved = useRef(false);
  useEffect(() => {
    if (terminalSetupNeeded && !terminalSetupShown.current) {
      terminalSetupShown.current = true;
      pushOverlay("configPanel", {initialTab: "general", initialSetting: "terminal"});
    }
  }, [terminalSetupNeeded, pushOverlay]);
  const initialTerminalPanelOpen = overlayStack.some(overlay =>
    overlay.type === "configPanel" && overlay.props?.initialSetting === "terminal");
  useEffect(() => {
    if (initialTerminalPanelOpen) terminalSetupObserved.current = true;
    else if (terminalSetupObserved.current) setTerminalSetupNeeded(false);
  }, [initialTerminalPanelOpen]);
  const onSettingSaved = useCallback(async (key: string) => {
    if (key !== "launcher.terminal" || !terminalSetupNeeded || !initialTerminalPanelOpen) return;
    const result = await onTerminalSetupComplete?.();
    if (result?.completed) setTerminalSetupNeeded(false);
    return result?.message;
  }, [terminalSetupNeeded, initialTerminalPanelOpen, onTerminalSetupComplete]);

  // First-launch: if no projects AND no global LLM provider, force LLM config.
  // Global provider fetch is async, so gate the decision on its completion.
  const [firstLaunchChecked, setFirstLaunchChecked] = useState(false);
  const [globalLLMFetched, setGlobalLLMFetched] = useState(false);
  useEffect(() => {
    let alive = true;
    fetchGlobalLLM().then(() => { if (alive) setGlobalLLMFetched(true); });
    return () => { alive = false; };
  }, [fetchGlobalLLM]);

  useEffect(() => {
    if (!terminalSetupNeeded && overlayStack.length === 0 && !loading && globalLLMFetched && !firstLaunchChecked &&
        projects.length === 0 && !activeProject && globalProviders.length === 0) {
      setFirstLaunchChecked(true);
      pushOverlay("configPanel", {initialTab: "providers"});
    }
  }, [terminalSetupNeeded, overlayStack.length, loading, globalLLMFetched, firstLaunchChecked, projects.length, activeProject, globalProviders.length, pushOverlay]);

  // ── Status text derivation ──────────────────────────────
  const [screenStatusText, setScreenStatusText] = useState(startupNotice);
  const [footerOverride, setFooterOverride] = useState<FooterConfig | null>(null);
  const [pendingDecision, setPendingDecision] = useState<PendingDecision | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [activeChatProcessId, setActiveChatProcessId] = useState("");
  const [activeChatRequestId, setActiveChatRequestId] = useState("");
  const sendChatRef = useRef<(text: string) => void>(() => {});
  const sendChatReadyRef = useRef(false);
  const manualCreateRef = useRef<(text: string) => boolean>(() => false);
  const chatScrollRef = useRef<ChatScrollHandle | null>(null);
  const processListIdsRef = useRef<string[]>([]);
  const runningBIdsRef = useRef<string[]>([]);
  const processesRef = useRef(processes);
  const [cacheSamples, setCacheSamples] = useState<Record<string, number | null>>({});

  useEffect(() => { processesRef.current = processes; }, [processes]);
  useEffect(() => {
    if (!(client instanceof NativeHostClient)) return;
    return client.subscribeEvents(event => {
      if (event.event === "config_changed") {
        notifyGeneralConfigChanged();
        return;
      }
      if (event.event === "config_error") {
        dispatch({type: "set_cmd_result", text: String(event.message || "Configuration was not applied")});
        return;
      }
      if (event.event !== "deletion_status") return;
      dispatch({type:"set_cmd_result", text:String(event.message || ("Deletion " + event.state))});
    });
  }, [client, dispatch]);
  useEffect(() => {
    const sample = () => {
      const next: Record<string, number | null> = {};
      for (const process of Object.values(processesRef.current)) {
        const summary = process.cache_summary;
        const eligible = Number(summary?.eligible_input_tokens || 0);
        const input = Number(summary?.input_tokens || 0);
        next[process.process_id] = eligible > 0
          ? Number(summary?.eligible_hit_ratio || 0) * 100
          : input > 0
            ? Number(summary?.raw_hit_ratio || 0) * 100
            : null;
      }
      setCacheSamples(next);
    };
    sample();
    const timer = setInterval(sample, 60_000);
    return () => clearInterval(timer);
  }, [activeProject]);
  // Give a newly opened or recovered process its first persisted cache sample
  // immediately. Existing entries still refresh only on the minute timer.
  useEffect(() => {
    setCacheSamples(current => {
      let changed = false;
      const next = {...current};
      for (const process of Object.values(processes)) {
        if (Object.prototype.hasOwnProperty.call(next, process.process_id)) continue;
        const summary = process.cache_summary;
        const eligible = Number(summary?.eligible_input_tokens || 0);
        const input = Number(summary?.input_tokens || 0);
        next[process.process_id] = eligible > 0
          ? Number(summary?.eligible_hit_ratio || 0) * 100
          : input > 0
            ? Number(summary?.raw_hit_ratio || 0) * 100
            : null;
        changed = true;
      }
      return changed ? next : current;
    });
  }, [processes]);

  // Running/pending B agents derived directly from the live process map (single
  // source of truth) — avoids the stale-callback indirection that left the footer
  // strip empty until a re-entry.
  const runningB = useMemo(() => runningWorkers(processes), [processes]);

  useEffect(() => {
    runningBIdsRef.current = runningB.map((p) => p.process_id);
  }, [runningB]);

  // Context utilization — leftmost in the bottom status bar, non-selectable.
  const mainAgentRuntimeStatus = useMemo(() => {
    const root = Object.values(processes)
      .filter((p) => p.parent_id === null)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    const parts = [processRuntimeStatus(root)];
    if (loopData.storage && loopData.storage.level !== "ok") {
      parts.push(`storage ${loopData.storage.level}`);
    }
    return parts.filter(Boolean).join(" · ");
  }, [processes, loopData.storage]);

  const mainAgentContextPct = useMemo(() => {
    const root = Object.values(processes)
      .filter((p) => p.parent_id === null)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    return contextPct(root?.estimated_tokens || 0, root?.context?.limit);
  }, [processes]);

  const mainAgentCachePct = useMemo(() => {
    const root = Object.values(processes)
      .filter((p) => p.parent_id === null)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    return root ? cacheSamples[root.process_id] ?? null : null;
  }, [processes, cacheSamples]);

  const activeAgentRuntimeStatus = useMemo(() => {
    const p = activeAgentId ? processes[activeAgentId] : null;
    return processRuntimeStatus(p || undefined);
  }, [activeAgentId, processes]);

  const activeAgentContextPct = useMemo(() => {
    const p = activeAgentId ? processes[activeAgentId] : null;
    return contextPct(p?.estimated_tokens || 0, p?.context?.limit);
  }, [activeAgentId, processes]);
  const activeAgentCachePct = activeAgentId ? cacheSamples[activeAgentId] ?? null : null;

  const derivedStatus = useMemo(() => {
    const online = projects.filter((p) => p.daemonOnline).length;
    const total = projects.length;
    const provCount = providers.length;
    switch (scene) {
      case "projects": {
        return generalConfig.language === "zh"
          ? `● ${online} 个运行时在线 · ${total} 个项目  |  ↑↓ 选择 · Enter 打开 · ${chordLabel("slash")} 指令 · /help 帮助`
          : `● ${online} active runtimes · ${total} projects  |  ↑↓ select · Enter open · ${chordLabel("slash")} commands · /help`;
      }
      case "workspace": {
        // Status text is rendered by RunningBStrip (single horizontal line).
        return "";
      }
      case "agent_detail": {
        const waitingQuestions = loopData.pendingQuestions.length;
        return [activeAgentRuntimeStatus, waitingQuestions ? `${waitingQuestions} question${waitingQuestions === 1 ? "" : "s"} pending` : "", "← A", "type to send feedback", "/ commands"]
          .filter(Boolean)
          .join(" · ");
      }
      case "process_list":
        return screenStatusText ||
          (provCount > 0
            ? `● ${online}/${total} daemons  |  ${provCount} providers`
            : `● ${online}/${total} daemons online  |  ${total} projects`);
      default:
        return `● ${online}/${total} daemons online  |  ${total} projects`;
    }
  }, [scene, projects, providers, screenStatusText, activeAgentRuntimeStatus, generalConfig.language, loopData.pendingQuestions.length]);

  const previousDecisionId = useRef("");
  useEffect(() => {
    const nextId = pendingDecision?.decision_id || "";
    if (nextId === previousDecisionId.current) return;
    previousDecisionId.current = nextId;
    const recommended = pendingDecision?.options.findIndex((option) => option.recommended) ?? -1;
    dispatch({ type: "set_decision_selection", index: recommended >= 0 ? recommended : 0 });
    dispatch({ type: "set_decision_composing", composing: false });
    dispatch({ type: "set_decision_submitting", submitting: false });
    textInput.setValue("");
  }, [pendingDecision, dispatch, textInput]);

  const handleDecisionChange = useCallback((decision: PendingDecision | null) => {
    setPendingDecision(decision);
  }, []);

  const handleSendSettled = useCallback(() => {
    dispatch({ type: "set_decision_submitting", submitting: false });
  }, [dispatch]);

  // ── Suggestions ─────────────────────────────────────────
  const suggestions = useMemo(() => {
    if (mode !== "COMMAND") return [];
    return getCommands(scene, cmdInput.value, generalConfig.language);
  }, [mode, cmdInput.value, scene, generalConfig.language]);

  // ── Command context (inject deps) ───────────────────────
  const visibleProcesses = useMemo(() => visibleBProcesses(processes), [processes]);
  const selectedProcessIndex = Math.min(Math.max(0, processListSelIdx), Math.max(0, visibleProcesses.length - 1));
  useEffect(() => {
    if (selectedProcessIndex !== processListSelIdx) dispatch({type: "select_process", index: selectedProcessIndex});
  }, [selectedProcessIndex, processListSelIdx, dispatch]);
  const cmdCtx: CommandContext = useMemo(() => ({
    client,
    projects,
    sel,
    activeProject,
    refresh,
    scene,
    selectedProcessId: scene === "process_list"
      ? visibleProcesses[selectedProcessIndex]?.process_id
      : undefined,
    refreshProcesses: loopData.refresh,
    createB: (text: string) => manualCreateRef.current(text),
    activeProcessId: scene === "agent_detail"
      ? (activeAgentId || undefined)
      : Object.values(processes)
          .filter((p) => p.parent_id === null)
          .sort((a, b) => b.created_at.localeCompare(a.created_at))[0]?.process_id,
  }), [client, projects, sel, activeProject, refresh, scene, activeAgentId, processes, visibleProcesses, selectedProcessIndex, loopData.refresh]);

  // ── Unified scene navigation ──────────────────────────
  const navigate = useCallback(
    (scene: Scene, patch: Partial<Pick<AppState, "activeProject" | "activeAgentId" | "processListSelIdx">> = {}) => {
      textInput.setValue("");
      cmdInput.setValue("");
      dispatch({ type: "navigate", scene, patch });
    },
    [dispatch, textInput, cmdInput],
  );

  // Explicit terminal smoke mode enters the real workspace scene and submits
  // one task through the same sendChatRef used by keyboard input.  It is inert
  // in normal launches and intentionally does not bypass App/ChatPanel render.
  const startupSmokeSent = useRef(false);
  const startupSmokeDecisionsAnswered = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!startupSmokeTask || terminalSetupNeeded || overlayStack.length || startupSmokeSent.current || loading || loopData.loading) return;
    if (!projects.some((item) => item.name === startupSmokeTask.project)) return;
    if (scene !== "workspace" || activeProject !== startupSmokeTask.project) {
      navigate("workspace", { activeProject: startupSmokeTask.project });
      return;
    }
    // A machine-generated option answer has authority only when the exact
    // durable decision identity has reached useChat.  Submitting earlier can
    // silently turn it into a fresh chat, while marking the one-shot smoke as
    // consumed before recovery finishes.
    if (
      startupSmokeTask.message.startsWith("Choose option ")
      && !pendingDecision
    ) return;
    let attempts = 0;
    const timer = setInterval(() => {
      if (startupSmokeSent.current) {
        clearInterval(timer);
        return;
      }
      attempts += 1;
      if (sendChatReadyRef.current) {
        if (startupSmokeTask.manualDelegation) {
          const accepted = manualCreateRef.current(startupSmokeTask.message);
          if (accepted) {
            startupSmokeSent.current = true;
            clearInterval(timer);
          }
        } else {
          startupSmokeSent.current = true;
          clearInterval(timer);
          sendChatRef.current(startupSmokeTask.message);
        }
      } else if (attempts >= 100) {
        clearInterval(timer);
      }
    }, 100);
    return () => clearInterval(timer);
  }, [
    startupSmokeTask, terminalSetupNeeded, overlayStack.length, loading, loopData.loading, projects, scene,
    activeProject, navigate, pendingDecision,
  ]);

  // Formal terminal smoke runs may automate only the user's selection; they
  // still exercise the normal durable decision UI and sendChat path.  Wait for
  // the permission card to exist first so execution can never precede consent.
  useEffect(() => {
    if (!startupSmokeTask?.autoAllowOnce || !startupSmokeSent.current
      || !pendingDecision
      || pendingDecision.kind !== "permission" || !sendChatReadyRef.current) return;
    const decisionId = pendingDecision.decision_id;
    if (!decisionId || startupSmokeDecisionsAnswered.current.has(decisionId)) return;
    const optionIndex = pendingDecision.options.findIndex(option => option.action === "allow_once");
    if (optionIndex < 0) return;
    startupSmokeDecisionsAnswered.current.add(decisionId);
    const timer = setTimeout(() => {
      sendChatRef.current(`Choose option ${optionIndex + 1}: ${pendingDecision.options[optionIndex]!.label}`);
    }, 150);
    return () => clearTimeout(timer);
  }, [startupSmokeTask, pendingDecision]);

  // ── Effect deps ──────────────────────────────────────────
  const runCommandDeps: RunCommandDeps = useMemo(() => ({
    dispatch,
    clearCmd: () => cmdInput.setValue(""),
    cmdCtx,
    projectNames: projects.map((p: any) => p.name),
    sel,
    activeProject,
  }), [dispatch, cmdCtx, projects, sel, activeProject, cmdInput]);

  const stopProcessDeps: StopProcessDeps = useMemo(() => ({
    dispatch,
    client,
    activeProject,
  }), [dispatch, client, activeProject]);

  // ── Interrupt target resolution (feeds keymap context) ──
  const resolveInterruptTarget = useCallback((): { pid?: string; requestId?: string; running: boolean } => {
    const interruptible = (status?: string) => [
      "running", "waiting", "awaiting_user", "recovering", "cancelling",
    ].includes(status || "");
    if (scene === "agent_detail") {
      const pid = activeAgentId ?? undefined;
      return { pid, running: pid ? interruptible(processes[pid]?.status) : false };
    }
    if (scene === "workspace") {
      if (activeChatProcessId) {
        return { pid: activeChatProcessId, requestId: activeChatRequestId || undefined,
          running: chatBusy || interruptible(processes[activeChatProcessId]?.status) };
      }
      if (chatBusy && activeChatRequestId) {
        return { requestId: activeChatRequestId, running: true };
      }
      const root = Object.values(processes)
        .filter((p) => p.parent_id === null && interruptible(p.status))
        .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
      return { pid: root?.process_id, running: interruptible(root?.status) };
    }
    return { pid: undefined, running: false };
  }, [scene, activeAgentId, processes, activeChatProcessId, activeChatRequestId, chatBusy]);

  // ── Input action appliers ────────────────────────────────
  const applyTextOp = useCallback((buffer: TextBuffer, op: TextOp) => {
    const buf = buffer === "cmd" ? cmdInput : buffer === "llm" ? llmCmdInput : textInput;
    applyTextOpToBuf(op, buf);
  }, [cmdInput, llmCmdInput, textInput]);

  const applyInputActions = useCallback((acts: InputAction[]) => {
    const prepared = acts.map(a => a.kind === "effect" && a.effect.type === "send_chat"
      ? {...a, effect: {...a.effect, text: textInput.materialize(a.effect.text)}} as InputAction
      : a);
    for (const a of prepared) {
      if (a.kind === "state") {
        dispatch(a.action);
      } else if (a.kind === "text") {
        applyTextOp(a.buffer, a.op);
      } else {
        const e = a.effect;
        if (e.type === "run_command") void runCommandEffect(e.cmd, runCommandDeps);
        else if (e.type === "stop_process") void stopProcessEffect(e.pid, stopProcessDeps);
        else if (e.type === "send_chat") sendChatRef.current(e.text);
        else if (e.type === "scroll_chat") chatScrollRef.current?.scrollBy(e.delta);
        else if (e.type === "scroll_chat_bottom") chatScrollRef.current?.scrollToBottom();
        else if (e.type === "edit_prompt_external") {
          void editPromptExternally(textInput.materialize(), generalConfig.externalEditor).then(value => {
            textInput.setExternalValue(value);
            dispatch({type: "set_chat_input_focused", focused: true});
            setScreenStatusText("Draft imported from external editor");
          }).catch(error => setScreenStatusText(`Editor failed: ${String(error)}`));
        }
        else if (e.type === "report_notice") for (const a of noticeToActions(e.code, e.params)) dispatch(a);
      }
    }
  }, [dispatch, applyTextOp, runCommandDeps, stopProcessDeps, textInput, generalConfig.externalEditor]);

  // ── Keyboard dispatch (global) ───────────────────────────
  useInput((input: string, key: any) => {
    const ctx: InputContext = {
      scene,
      mode,
      chatInputFocused,
      cmdValue: cmdInput.value,
      cmdCursor: cmdInput.cursor,
      textValue: textInput.value,
      textCursor: textInput.cursor,
      textVisualWidth: Math.max(20, Math.max(60, termCols || 80) - 4),
      projectsLength: projects.length,
      projectNames: projects.map((p: any) => p.name),
      sel,
      activeProject,
      processListIds: processListIdsRef.current,
      processListSelIdx,
      runningBIds: runningBIdsRef.current,
      runningBSelIdx,
      statusBarFocused,
      decisionOptionLabels: pendingDecision?.options.map((option) => option.label) || [],
      decisionAllowFreeForm: Boolean(pendingDecision?.allow_free_form),
      decisionSelection,
      decisionComposing,
      decisionSubmitting,
      chatBusy: scene === "workspace" ? chatBusy : false,
      suggestions,
      suggestionIdx,
      cmdHistory: appStore.getState().cmdHistory,
      cmdHistoryIdx: appStore.getState().cmdHistoryIdx,
      interruptTarget: resolveInterruptTarget(),
    };
    applyInputActions(resolveSceneKey(ctx, input, key));
  }, { isActive: overlayStack.length === 0 });

  // ── Render ───────────────────────────────────────────────
  const w = Math.max(60, termCols || 80);
  const h = termRows || 24;

  // Effective footer: overlay override → auto-hide for non-footer overlays → default
  const effectiveFooterOverride = useMemo(() => {
    if (footerOverride) return footerOverride;
    const inline = ["btwPanel", "compactPanel"].includes(
      overlayStack[overlayStack.length - 1]?.type || "",
    );
    if (overlayStack.length > 0 && !inline) {
      return { hidden: true } as const;
    }
    return null;
  }, [footerOverride, overlayStack]);

  const overlayCtx = {
    client,
    scene,
    activeProject,
    w,
    h,
    llmCmdInput,
    cmdInput,
    dispatch,
    popOverlay,
    refresh,
    navigate,
    setFooterOverride,
    setScreenStatusText,
    exit,
    projects,
    runCommandEffect,
    runCommandDeps,
    toolEvents: loopData.toolEvents,
    processes: loopData.processes,
    sendChat: (text: string) => sendChatRef.current(text),
    verbose: generalConfig.verbose,
    onSettingSaved,
  };
  const topOverlay = overlayStack[overlayStack.length - 1];
  const inlineOverlay = ["btwPanel", "compactPanel"].includes(topOverlay?.type || "");
  const footerDrawer = inlineOverlay
    ? <ScrollableInputLayer>{renderOverlay(topOverlay, overlayCtx)}</ScrollableInputLayer>
    : null;

  return (
    <Box flexDirection="column" width={w} height={h} flexShrink={0}>
      {error ? (
        <Box paddingLeft={1}><Text color={colors.danger}>Error: {error}</Text></Box>
      ) : null}
      {activeProject && loopData.error ? (
        <Box paddingLeft={1}><Text color={colors.danger}>Runtime status unavailable: {loopData.error}</Text></Box>
      ) : null}

      <Box flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden" display={scene === "workspace" && (!topOverlay || inlineOverlay) ? "none" : "flex"}>
      {overlayStack.length > 0 && !inlineOverlay ? (
        <ScrollableInputLayer>{renderOverlay(topOverlay, overlayCtx)}</ScrollableInputLayer>
      ) : loading ? (
        <Box paddingLeft={1} paddingTop={1}><Text dimColor>Loading projects...</Text></Box>
      ) : scene === "process_list" && activeProject ? (
        <ProcessList
          project={activeProject}
          loopData={loopData}
          cols={w}
          selIdx={selectedProcessIndex}
          idsRef={processListIdsRef}
          onStatusUpdate={setScreenStatusText}
        />
      ) : scene === "agent_detail" ? (
        <AgentDetailScene
          client={client}
          loopData={loopData}
          activeProject={activeProject}
          activeAgentId={activeAgentId}
          cols={w}
          rows={h}
          sendChatRef={sendChatRef}
          scrollChatRef={chatScrollRef}
          verbose={generalConfig.verbose}
          decisionSelection={decisionSelection}
          decisionComposing={decisionComposing}
          decisionSubmitting={decisionSubmitting}
          onDecisionChange={handleDecisionChange}
          onSendSettled={handleSendSettled}
          onContinueAgent={(processId) => navigate("agent_detail", {activeAgentId: processId})}
        />
      ) : scene === "workspace" && activeProject ? null : (
        <Overview projects={projects} sel={sel} mode={mode} cols={w} listActive={!cmdInput.value.startsWith("/")} language={generalConfig.language} />
      )}
      </Box>

      {/* Keep the project conversation controller alive while inspecting B or a
          modal. Presentation changes must not discard an in-flight A stream. */}
      {activeProject ? <Box flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden" display={scene === "workspace" && (!topOverlay || inlineOverlay) ? "flex" : "none"}>
        <ProjectWorkspace
          key={activeProject}
          manualCreateRef={manualCreateRef}
          visible={scene === "workspace" && (!topOverlay || inlineOverlay)}
          project={activeProject}
          projectId={projects.find((item) => item.name === activeProject)?.project_id}
          workspace={projects.find((item) => item.name === activeProject)?.workspace}
          client={client}
          loopData={loopData}
          cols={w}
          rows={h}
          onBack={() => navigate("projects")}
          onEnterAgent={(processId: string) => navigate("agent_detail", { activeAgentId: processId })}
          refreshKey={refreshKey}
          sendChatRef={sendChatRef}
          sendChatReadyRef={sendChatReadyRef}
          scrollChatRef={chatScrollRef}
          verbose={generalConfig.verbose}
          decisionSelection={decisionSelection}
          decisionComposing={decisionComposing}
          decisionSubmitting={decisionSubmitting}
          onDecisionChange={handleDecisionChange}
          onSendSettled={handleSendSettled}
          onBusyChange={setChatBusy}
          onActiveProcessChange={setActiveChatProcessId}
          onActiveRequestChange={setActiveChatRequestId}
        />
      </Box> : null}

      <CommandBar
        width={w}
        mode={mode}
        textInput={textInput}
        cmdInput={cmdInput}
        cmdResult={cmdResult}
        statusText={derivedStatus}
        suggestions={suggestions}
        suggestionIdx={suggestionIdx}
        scene={scene}
        footerOverride={effectiveFooterOverride}
        runningB={scene === "workspace" ? runningB : undefined}
        runningBSelIdx={runningBSelIdx}
        statusBarFocused={scene === "workspace" ? statusBarFocused : false}
        contextPct={scene === "workspace" ? mainAgentContextPct : scene === "agent_detail" ? activeAgentContextPct : ""}
        cachePct={scene === "workspace" ? mainAgentCachePct : scene === "agent_detail" ? activeAgentCachePct : null}
        footerDrawer={footerDrawer}
        language={generalConfig.language}
      />
    </Box>
  );
}
