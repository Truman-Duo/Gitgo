// src/components/App.tsx — Three-scene routing with createStore
import React, { useCallback, useMemo } from "react";
import { Box, Text, useInput, useApp, useTerminalSize } from "@anthropic/ink";
import { McpClient } from "../mcp/client.js";
import { useGitgoData } from "../hooks/useGitgoData.js";
import { createStore, useStore, initialAppState, type AppState } from "../state/store.js";
import { executeCommand, COMMANDS, type CommandContext } from "../commands.js";
import { Overview } from "./Overview.js";
import { ProjectWorkspace } from "./ProjectWorkspace.js";
import { AgentDetail, AgentDetailScene } from "./AgentDetail.js";
import { LLMConfigPanel } from "./LLMConfigPanel.js";
import { CommandBar, type Suggestion } from "./CommandBar.js";
import { HelpPanel } from "./HelpPanel.js";

type Props = { client: McpClient; refreshSec?: number };

const appStore = createStore<AppState>(initialAppState());

export function App({ client, refreshSec = 5 }: Props) {
  const { exit } = useApp();
  const { columns: termCols, rows: termRows } = useTerminalSize();
  const { projects, loading, error, refresh } = useGitgoData(client, refreshSec);

  const scene = useStore(appStore, (s) => s.scene);
  const previousScene = useStore(appStore, (s) => s.previousScene);
  const activeProject = useStore(appStore, (s) => s.activeProject);
  const activeAgentId = useStore(appStore, (s) => s.activeAgentId);
  const sel = useStore(appStore, (s) => s.sel);
  const focus = useStore(appStore, (s) => s.focus);
  const cmdBuf = useStore(appStore, (s) => s.cmdBuf);
  const cmdCursor = useStore(appStore, (s) => s.cmdCursor);
  const cmdResult = useStore(appStore, (s) => s.cmdResult);
  const showHelp = useStore(appStore, (s) => s.showHelp);
  const suggestionIdx = useStore(appStore, (s) => s.suggestionIdx);
  const refreshKey = useStore(appStore, (s) => s.refreshKey);

  const setState = appStore.setState;

  // ── Suggestions ─────────────────────────────────────────
  const isCommand = cmdBuf.startsWith(":");
  const suggestions = useMemo(() => {
    if (focus !== "command" || !isCommand) return [];
    const word = cmdBuf.slice(1).trim().toLowerCase();
    const seen = new Set<string>();
    return COMMANDS.filter((c) => {
      if (seen.has(c.label)) return false;
      if (word && !c.label.startsWith(word)) return false;
      seen.add(c.label);
      return true;
    });
  }, [focus, cmdBuf, isCommand]);

  // ── Command context (inject deps) ───────────────────────
  const cmdCtx: CommandContext = useMemo(() => ({
    client,
    projects,
    sel,
    refresh,
  }), [client, projects, sel, refresh]);

  // ── Scene navigation callbacks ──────────────────────────
  const enterScene = useCallback(
    (s: AppState["scene"], project: string | null = null) => {
      setState((prev) => ({ ...prev, scene: s, activeProject: project, activeAgentId: null }));
    },
    [setState],
  );

  const handleEnterProject = useCallback(() => {
    if (projects[sel]) enterScene("workspace", projects[sel].name);
  }, [projects, sel, enterScene]);

  const handleBackToProjects = useCallback(() => enterScene("projects"), [enterScene]);

  const handleEnterAgent = useCallback(
    (processId: string) => {
      setState((prev) => ({ ...prev, scene: "agent_detail", activeAgentId: processId }));
    },
    [setState],
  );

  const handleBackFromAgent = useCallback(() => {
    setState((prev) => ({ ...prev, scene: "workspace", activeAgentId: null }));
  }, [setState]);

  const openLLMConfig = useCallback(() => {
    setState((prev) => ({ ...prev, previousScene: prev.scene, scene: "llm_config" }));
  }, [setState]);

  const handleBackFromLLM = useCallback(() => {
    setState((prev) => ({ ...prev, scene: prev.previousScene }));
  }, [setState]);

  // ── Command execution ────────────────────────────────────
  const runCommand = useCallback(async (cmd: string) => {
    setState((prev) => ({ ...prev, cmdHistory: [...prev.cmdHistory, cmd], cmdHistoryIdx: -1 }));
    // Handle :llm / :config locally (opens LLM config panel)
    const clean = cmd.replace(/^:\s*/, "").split(/\s+/)[0]?.toLowerCase();
    if (clean === "llm" || clean === "config" || clean === "lcfg") {
      setState((prev) => ({ ...prev, previousScene: prev.scene, scene: "llm_config", focus: "table", cmdBuf: "", cmdCursor: 0 }));
      return;
    }
    const outcome = await executeCommand(cmd, cmdCtx);
    const updates: Partial<AppState> = { focus: "table", cmdResult: outcome.resultText };
    if (outcome.showHelp) updates.showHelp = true;
    if (outcome.refreshTrigger) updates.refreshKey = refreshKey + 1;
    if (outcome.jumpToProject !== undefined) updates.sel = outcome.jumpToProject;
    setState((prev) => ({ ...prev, ...updates }));
  }, [cmdCtx, setState, refreshKey]);

  // ── Keyboard dispatch (global) ───────────────────────────
  useInput((input: string, key: any) => {
    if (focus === "command") return handleCommandInput(input, key, cmdBuf, cmdCursor, suggestions, suggestionIdx, runCommand, setState);
    if (showHelp) {
      if (key.escape || input === "h" || input === "q") setState((p) => ({ ...p, showHelp: false }));
      return;
    }
    if (scene === "llm_config") {
      if (key.escape || input === "q") { handleBackFromLLM(); return; }
      return;
    }
    if (scene === "agent_detail") {
      if (key.escape || input === "q") { handleBackFromAgent(); return; }
      return;
    }
    if (scene === "workspace") {
      if (key.escape || input === "q") { handleBackToProjects(); return; }
      if (input === ":") { enterCommandMode(setState); return; }
      if (input === "L" || input === "l") { openLLMConfig(); return; }
      return;
    }
    // Projects scene
    if (input === "q") { exit(); return; }
    if (input === ":") { enterCommandMode(setState); return; }
    if (input === "h") { setState((p) => ({ ...p, showHelp: !p.showHelp })); return; }
    if (input === "L" || input === "l") { openLLMConfig(); return; }
    if (key.return && projects.length > 0) { handleEnterProject(); return; }
    if (key.upArrow) {
      setState((p) => ({
        ...p,
        sel: p.sel === 0 ? 0 : p.sel - 1,
        focus: p.sel === 0 ? "command" : p.focus,
        cmdBuf: p.sel === 0 ? "" : p.cmdBuf,
        cmdCursor: 0,
      }));
      return;
    }
    if (key.downArrow) {
      setState((p) => ({
        ...p,
        sel: p.sel >= projects.length - 1 ? projects.length - 1 : p.sel + 1,
        focus: p.sel >= projects.length - 1 ? "command" : p.focus,
        cmdBuf: p.sel >= projects.length - 1 ? "" : p.cmdBuf,
        cmdCursor: 0,
      }));
      return;
    }
  });

  // ── Render ───────────────────────────────────────────────
  if (loading) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text dimColor>Loading projects...</Text>
      </Box>
    );
  }

  const w = Math.max(60, termCols || 80);
  const h = termRows || 24;

  return (
    <Box flexDirection="column" width={w}>
      {error ? (
        <Box paddingLeft={1}><Text color="red">Error: {error}</Text></Box>
      ) : null}

      {showHelp ? (
        <HelpPanel onDismiss={() => setState((p) => ({ ...p, showHelp: false }))} />
      ) : scene === "llm_config" && activeProject ? (
        <LLMConfigPanel
          client={client}
          project={activeProject}
          cols={w}
          rows={h}
          onBack={handleBackFromLLM}
        />
      ) : scene === "agent_detail" ? (
        <AgentDetailScene
          client={client}
          activeProject={activeProject}
          activeAgentId={activeAgentId}
          cols={w}
          rows={h}
        />
      ) : scene === "workspace" && activeProject ? (
        <ProjectWorkspace
          project={activeProject}
          client={client}
          cols={w}
          rows={h}
          onBack={handleBackToProjects}
          onEnterAgent={handleEnterAgent}
          refreshKey={refreshKey}
        />
      ) : (
        <Overview projects={projects} sel={sel} focus={focus} cols={w} />
      )}

      <Box flexGrow={1} />

      <Box marginTop={1}>
        <CommandBar
          buf={focus === "command" ? cmdBuf : null}
          cursor={cmdCursor}
          result={cmdResult}
          suggestions={suggestions}
          suggestionIdx={suggestionIdx}
          cols={w}
        />
      </Box>
    </Box>
  );
}

// ── Helpers ─────────────────────────────────────────────────

function enterCommandMode(setState: (updater: (prev: AppState) => AppState) => void) {
  setState((p) => ({ ...p, focus: "command", cmdBuf: "", cmdCursor: 0, cmdResult: "", suggestionIdx: 0 }));
}

function handleCommandInput(
  input: string,
  key: any,
  cmdBuf: string,
  cmdCursor: number,
  suggestions: Suggestion[],
  suggestionIdx: number,
  runCommand: (cmd: string) => Promise<void>,
  setState: (updater: (prev: AppState) => AppState) => void,
) {
  if (key.return) {
    const cmd = cmdBuf.trim();
    if (cmd) runCommand(cmd);
    else setState((p) => ({ ...p, focus: "table", cmdBuf: "", cmdCursor: 0, suggestionIdx: 0 }));
    return;
  }
  if (key.escape) {
    setState((p) => ({ ...p, focus: "table", cmdBuf: "", cmdCursor: 0, cmdHistoryIdx: -1, suggestionIdx: 0 }));
    return;
  }
  if (key.leftArrow)  { setState((p) => ({ ...p, cmdCursor: Math.max(0, p.cmdCursor - 1) })); return; }
  if (key.rightArrow) { setState((p) => ({ ...p, cmdCursor: Math.min(cmdBuf.length, p.cmdCursor + 1) })); return; }
  if (key.home)       { setState((p) => ({ ...p, cmdCursor: 0 })); return; }
  if (key.end)        { setState((p) => ({ ...p, cmdCursor: cmdBuf.length })); return; }
  if (key.backspace) {
    if (cmdCursor > 0) {
      setState((p) => ({ ...p, cmdBuf: cmdBuf.slice(0, cmdCursor - 1) + cmdBuf.slice(cmdCursor), cmdCursor: p.cmdCursor - 1 }));
    }
    return;
  }
  if (key.delete) {
    if (cmdCursor < cmdBuf.length) {
      setState((p) => ({ ...p, cmdBuf: cmdBuf.slice(0, cmdCursor) + cmdBuf.slice(cmdCursor + 1) }));
    }
    return;
  }
  if (key.upArrow) {
    if (!cmdBuf.trim()) { setState((p) => ({ ...p, focus: "table", cmdBuf: "", cmdCursor: 0, suggestionIdx: 0 })); return; }
    if (suggestions.length > 0) {
      setState((p) => ({ ...p, suggestionIdx: (p.suggestionIdx - 1 + suggestions.length) % suggestions.length }));
      return;
    }
    setState((p) => ({ ...p, focus: "table", cmdBuf: "", cmdCursor: 0, suggestionIdx: 0 }));
    return;
  }
  if (key.downArrow && suggestions.length > 0) {
    setState((p) => ({ ...p, suggestionIdx: (p.suggestionIdx + 1) % suggestions.length }));
    return;
  }
  if (key.tab && suggestions.length > 0) {
    const s = suggestions[suggestionIdx % suggestions.length];
    if (s) {
      setState((p) => ({ ...p, cmdBuf: ":" + s.label + " ", cmdCursor: s.label.length + 2, suggestionIdx: p.suggestionIdx + 1 }));
    }
    return;
  }
  if (input && input.length >= 1 && !key.ctrl && !key.meta) {
    setState((p) => ({ ...p, cmdBuf: cmdBuf.slice(0, cmdCursor) + input + cmdBuf.slice(cmdCursor), cmdCursor: p.cmdCursor + input.length }));
    return;
  }
}
