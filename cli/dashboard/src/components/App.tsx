// src/components/App.tsx — Three-scene routing with createStore
import React, { useCallback, useMemo, useState, useRef, useEffect } from "react";
import { Box, Text, useInput, useApp, useTerminalSize } from "@anthropic/ink";
import { execSync } from "node:child_process";
import { McpClient } from "../mcp/client.js";
import { colors, contextPct } from "../theme/index.js";
import { useGitgoData } from "../hooks/useGitgoData.js";
import { useLoopData, type ProcessInfo } from "../hooks/useLoopData.js";
import { useLLMConfig } from "../hooks/useLLMConfig.js";
import { createReducerStore, reducer, useStore, initialAppState, type AppState, type AppAction, type OverlayType, type Scene } from "../state/store.js";
import { getCommands, type CommandContext } from "../commands.js";
import { resolveSceneKey, type InputContext, type InputAction, type TextBuffer, type TextOp } from "../input/keymap.js";
import type { ChatScrollHandle } from "../types.js";
import { runCommandEffect, stopProcessEffect, type RunCommandDeps, type StopProcessDeps } from "../effects/run.js";
import { noticeToActions } from "../notices.js";
import { useTextInput, type UseTextInputReturn, applyTextOp as applyTextOpToBuf } from "../hooks/useTextInput.js";
import { Overview } from "./Overview.js";
import { ProjectWorkspace } from "./ProjectWorkspace.js";
import { ProcessList } from "./ProcessList.js";
import { AgentDetail, AgentDetailScene } from "./AgentDetail.js";
import { ConfigPanel } from "./ConfigPanel.js";
import { CommandBar, type FooterConfig } from "./CommandBar.js";
import { HelpPanel } from "./HelpPanel.js";
import { QuitPanel } from "./QuitPanel.js";
import { CreateProjectPanel } from "./CreateProjectPanel.js";
import { StatusPanel } from "./StatusPanel.js";
import { ExportPanel } from "./ExportPanel.js";
import { GovernancePanel } from "./GovernancePanel.js";
import { MemoryPanel } from "./MemoryPanel.js";
import { TrialPanel } from "./TrialPanel.js";
import { FormalPanel } from "./FormalPanel.js";
import { LessonsPanel } from "./LessonsPanel.js";
import { RuntimeMenu } from "./RuntimeMenu.js";
import { InlineContext } from "./InlineContext.js";
import { DialogSelect, type DialogItem } from "./DialogSelect.js";
import { REGISTRY, getKeybindings } from "../keybindings.js";
import { chordLabel } from "../input/bindings.js";

type Props = { client: McpClient; refreshSec?: number };

const appStore = createReducerStore<AppState, AppAction>(reducer, initialAppState());

export function App({ client, refreshSec = 5 }: Props) {
  const { exit } = useApp();
  const { columns: termCols, rows: termRows } = useTerminalSize();
  const { projects, loading, error, refresh } = useGitgoData(client, refreshSec);
  const { providers: globalProviders, fetchStatus: fetchGlobalLLM } = useLLMConfig(client);

  const scene = useStore(appStore, (s) => s.scene);
  const activeProject = useStore(appStore, (s) => s.activeProject);
  const { providers, processes } = useLoopData(client, activeProject, 2); // header + interrupt target + running-B strip
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
    if (!loading && globalLLMFetched && !firstLaunchChecked &&
        projects.length === 0 && !activeProject && globalProviders.length === 0) {
      setFirstLaunchChecked(true);
      pushOverlay("configPanel");
    }
  }, [loading, globalLLMFetched, firstLaunchChecked, projects.length, activeProject, globalProviders.length, pushOverlay]);

  // ── Status text derivation ──────────────────────────────
  const [screenStatusText, setScreenStatusText] = useState("");
  const [footerOverride, setFooterOverride] = useState<FooterConfig | null>(null);
  const sendChatRef = useRef<(text: string) => void>(() => {});
  const chatScrollRef = useRef<ChatScrollHandle | null>(null);
  const processListIdsRef = useRef<string[]>([]);
  const runningBIdsRef = useRef<string[]>([]);

  // Running/pending B agents derived directly from the live process map (single
  // source of truth) — avoids the stale-callback indirection that left the footer
  // strip empty until a re-entry.
  const runningB = useMemo(() =>
    Object.values(processes)
      .filter((p) => p.ring_level === 3 && (p.status === "running" || p.status === "waiting"))
      .sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [processes]);

  useEffect(() => {
    runningBIdsRef.current = runningB.map((p) => p.process_id);
  }, [runningB]);

  // Context utilization — leftmost in the bottom status bar, non-selectable.
  const mainAgentCtxPct = useMemo(() => {
    const root = Object.values(processes).find(
      (p) => p.parent_id === null && (p.status === "running" || p.status === "waiting"),
    );
    return root ? contextPct(root.estimated_tokens) : "";
  }, [processes]);

  const activeAgentCtxPct = useMemo(() => {
    const p = activeAgentId ? processes[activeAgentId] : null;
    return p ? contextPct(p.estimated_tokens) : "";
  }, [activeAgentId, processes]);

  const derivedStatus = useMemo(() => {
    const online = projects.filter((p) => p.daemonOnline).length;
    const total = projects.length;
    const provCount = providers.length;
    switch (scene) {
      case "projects": {
        return `● ${online}/${total} daemons online  |  ${total} projects  |  ${chordLabel("slash")} for commands`;
      }
      case "workspace": {
        // Status text is rendered by RunningBStrip (single horizontal line).
        return "";
      }
      case "agent_detail": {
        return activeAgentCtxPct;
      }
      case "process_list":
        return screenStatusText ||
          (provCount > 0
            ? `● ${online}/${total} daemons  |  ${provCount} providers`
            : `● ${online}/${total} daemons online  |  ${total} projects`);
      default:
        return `● ${online}/${total} daemons online  |  ${total} projects`;
    }
  }, [scene, projects, providers, screenStatusText, activeAgentCtxPct]);

  // ── Suggestions ─────────────────────────────────────────
  const suggestions = useMemo(() => {
    if (mode !== "COMMAND") return [];
    return getCommands(scene, cmdInput.value);
  }, [mode, cmdInput.value, scene]);

  // ── Command context (inject deps) ───────────────────────
  const cmdCtx: CommandContext = useMemo(() => ({
    client,
    projects,
    sel,
    activeProject,
    refresh,
    scene,
  }), [client, projects, sel, activeProject, refresh, scene]);

  // ── Unified scene navigation ──────────────────────────
  const navigate = useCallback(
    (scene: Scene, patch: Partial<Pick<AppState, "activeProject" | "activeAgentId" | "processListSelIdx">> = {}) => {
      textInput.setValue("");
      cmdInput.setValue("");
      dispatch({ type: "navigate", scene, patch });
    },
    [dispatch, textInput, cmdInput],
  );

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
  const resolveInterruptTarget = useCallback((): { pid?: string; running: boolean } => {
    if (scene === "agent_detail") {
      const pid = activeAgentId ?? undefined;
      return { pid, running: pid ? processes[pid]?.status === "running" : false };
    }
    if (scene === "workspace") {
      const root = Object.values(processes).find((p) => p.parent_id === null);
      return { pid: root?.process_id, running: root?.status === "running" };
    }
    return { pid: undefined, running: false };
  }, [scene, activeAgentId, processes]);

  // ── Input action appliers ────────────────────────────────
  const applyTextOp = useCallback((buffer: TextBuffer, op: TextOp) => {
    const buf = buffer === "cmd" ? cmdInput : buffer === "llm" ? llmCmdInput : textInput;
    applyTextOpToBuf(op, buf);
  }, [cmdInput, llmCmdInput, textInput]);

  const applyInputActions = useCallback((acts: InputAction[]) => {
    for (const a of acts) {
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
        else if (e.type === "report_notice") for (const a of noticeToActions(e.code, e.params)) dispatch(a);
      }
    }
  }, [dispatch, applyTextOp, runCommandDeps, stopProcessDeps]);

  // ── Keyboard dispatch (global) ───────────────────────────
  useInput((input: string, key: any) => {
    // P0.5: Paste — clipboard read is a side effect, handled here not in keymap.
    if (key.ctrl && (input === "v" || input === "V")) {
      const clip = readClipboard();
      if (clip) applyTextOp(mode === "COMMAND" ? "cmd" : "text", { op: "insert", text: clip });
      return;
    }

    const ctx: InputContext = {
      scene,
      mode,
      chatInputFocused,
      cmdValue: cmdInput.value,
      cmdCursor: cmdInput.cursor,
      textValue: textInput.value,
      textCursor: textInput.cursor,
      projectsLength: projects.length,
      projectNames: projects.map((p: any) => p.name),
      sel,
      activeProject,
      processListIds: processListIdsRef.current,
      processListSelIdx,
      runningBIds: runningBIdsRef.current,
      runningBSelIdx,
      statusBarFocused,
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
    if (overlayStack.length > 0) return { hidden: true } as const;
    return null;
  }, [footerOverride, overlayStack.length]);

  function renderOverlay(overlay: { type: string; props?: Record<string, any> }) {
    switch (overlay.type) {
      case "help":
        return <HelpPanel scene={scene} onDismiss={popOverlay} />;
      case "quitConfirm":
        return (
          <QuitPanel
            onSaveAndQuit={() => {
              // Notify daemon, then exit gracefully
              popOverlay();
              exit();
            }}
            onForceQuit={() => {
              popOverlay();
              exit();
            }}
            onCancel={popOverlay}
          />
        );
      case "context": {
        const ctxProject = overlay.props?.project || activeProject;
        return ctxProject ? (
          <InlineContext project={ctxProject} client={client} cols={w}
            toolEvents={[]} initialTab={overlay.props?.initialTab ?? 0} onDismiss={popOverlay} />
        ) : null;
      }
      case "configPanel": {
        const llmProject = overlay.props?.project || activeProject || "";
        return (
          <ConfigPanel client={client} project={llmProject}
            cols={w} rows={h}
            initialTab={overlay.props?.initialTab ?? "providers"}
            cmdInput={llmCmdInput}
            onFooter={setFooterOverride}
            onBack={popOverlay}
            onStatusUpdate={setScreenStatusText}
            onRefresh={refresh} />
        );
      }
      case "whichkey": {
        const bindings = getKeybindings(scene);
        const items: DialogItem[] = bindings.map((c) => ({
          id: c.name,
          title: c.slashName,
          category: c.category,
          hint: c.keys.length > 0 ? c.keys.join(" ") : undefined,
        }));
        return (
          <DialogSelect
            items={items}
            onSelect={(id) => {
              popOverlay();
              const def = bindings.find((c) => c.name === id);
              if (def) {
                enterCommandMode(cmdInput, dispatch);
                cmdInput.setValue("/" + def.slashName);
              }
            }}
            onDismiss={popOverlay}
            title="Which Key?"
            placeholder="Filter commands..."
            height={h}
          />
        );
      }
      case "dialogSelect": {
        const cmds = getCommands(scene);
        const items: DialogItem[] = cmds.map((c) => ({
          id: c.label,
          title: c.label,
          category: "Commands",
          hint: c.description,
        }));
        return (
          <DialogSelect
            items={items}
            onSelect={(id) => {
              popOverlay();
              enterCommandMode(cmdInput, dispatch);
              cmdInput.setValue(id);
            }}
            onDismiss={popOverlay}
            title="Command Palette"
            placeholder="Type command..."
            height={h}
          />
        );
      }
      case "createForm":
        return (
          <CreateProjectPanel
            client={client}
            defaultWorkspace={process.cwd()}
            onDismiss={popOverlay}
            onCreated={() => { refresh(); popOverlay(); }}
            cmdInput={llmCmdInput}
            onFooter={setFooterOverride}
          />
        );
      case "statusPanel":
        return (
          <StatusPanel
            projects={projects}
            cols={w}
            onDismiss={popOverlay}
            onEnterProject={(name) => {
              popOverlay();
              navigate("workspace", { activeProject: name, activeAgentId: null });
            }}
          />
        );
      case "exportPanel": {
        const exportProject = overlay.props?.project || activeProject;
        return exportProject ? (
          <ExportPanel
            client={client}
            project={exportProject}
            cols={w}
            onDismiss={popOverlay}
          />
        ) : null;
      }
      case "governancePanel": {
        const govProject = overlay.props?.project || activeProject;
        return govProject ? (
          <GovernancePanel
            client={client}
            project={govProject}
            cols={w}
            initialTab={overlay.props?.initialTab ?? 0}
            onDismiss={popOverlay}
          />
        ) : null;
      }
      case "memoryPanel": {
        const memProject = overlay.props?.project || activeProject;
        return memProject ? (
          <MemoryPanel client={client} project={memProject} cols={w} onDismiss={popOverlay} />
        ) : null;
      }
      case "trialPanel": {
        const trialProject = overlay.props?.project || activeProject;
        return trialProject ? (
          <TrialPanel client={client} project={trialProject} cols={w} onDismiss={popOverlay} />
        ) : null;
      }
      case "formalPanel": {
        const formalProject = overlay.props?.project || activeProject;
        return formalProject ? (
          <FormalPanel client={client} project={formalProject} cols={w} onDismiss={popOverlay} />
        ) : null;
      }
      case "lessonsPanel": {
        const lessonsProject = overlay.props?.project || activeProject;
        return lessonsProject ? (
          <LessonsPanel client={client} project={lessonsProject} cols={w}
            initialQuery={overlay.props?.initialQuery} onDismiss={popOverlay} />
        ) : null;
      }
      case "runtimeMenu":
        return (
          <RuntimeMenu
            cols={w}
            rows={h}
            onSelect={(sub) => {
              popOverlay();
              void runCommandEffect("/runtime " + sub, runCommandDeps);
            }}
            onDismiss={popOverlay}
          />
        );
      default:
        return null;
    }
  }

  return (
    <Box flexDirection="column" width={w} flexGrow={1}>
      {error ? (
        <Box paddingLeft={1}><Text color={colors.danger}>Error: {error}</Text></Box>
      ) : null}

      <Box flexGrow={1}>
      {overlayStack.length > 0 ? (
        renderOverlay(overlayStack[overlayStack.length - 1])
      ) : loading ? (
        <Box paddingLeft={1} paddingTop={1}><Text dimColor>Loading projects...</Text></Box>
      ) : scene === "process_list" && activeProject ? (
        <ProcessList
          client={client}
          project={activeProject}
          cols={w}
          selIdx={processListSelIdx}
          idsRef={processListIdsRef}
          onStatusUpdate={setScreenStatusText}
        />
      ) : scene === "agent_detail" ? (
        <AgentDetailScene
          client={client}
          activeProject={activeProject}
          activeAgentId={activeAgentId}
          cols={w}
          rows={h}
          sendChatRef={sendChatRef}
          scrollChatRef={chatScrollRef}
        />
      ) : scene === "workspace" && activeProject ? (
        <ProjectWorkspace
          project={activeProject}
          client={client}
          cols={w}
          rows={h}
          onBack={() => navigate("projects")}
          onEnterAgent={(processId: string) => navigate("agent_detail", { activeAgentId: processId })}
          refreshKey={refreshKey}
          sendChatRef={sendChatRef}
          scrollChatRef={chatScrollRef}
        />
      ) : (
        <Overview projects={projects} sel={sel} mode={mode} cols={w} listActive={!cmdInput.value.startsWith("/")} />
      )}
      </Box>

      <CommandBar
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
        contextPct={scene === "workspace" ? mainAgentCtxPct : ""}
      />
    </Box>
  );
}

// ── Helpers ─────────────────────────────────────────────────

function enterCommandMode(
  cmdInput: UseTextInputReturn,
  dispatch: (action: AppAction) => void,
) {
  cmdInput.setValue("");
  dispatch({ type: "enter_command" });
}

function readClipboard(): string {
  let text = "";
  try {
    text = execSync(
      process.platform === "win32"
        ? "powershell -NoProfile -Command Get-Clipboard"
        : process.platform === "darwin"
        ? "pbpaste"
        : "xclip -o -selection clipboard",
      { encoding: "utf-8" }
    )
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
  } catch (_) {}
  return text;
}
