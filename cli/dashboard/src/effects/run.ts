// src/effects/run.ts — async effect runner.
// Translates command execution into AppActions and dispatches them.
// Kept free of React so it can be unit-tested in isolation.

import type { AppAction, Scene } from "../state/store.js";
import { isChatScene } from "../state/store.js";
import {
  executeCommand,
  findRootScene,
  getCommands,
  type CommandContext,
  type CommandOutcome,
} from "../commands.js";
import { noticeToActions } from "../notices.js";
import { stopProcess } from "../mcp/tools.js";
import { getDaemonClient } from "../clients.js";
import type { McpClient } from "../mcp/client.js";

function sceneLabel(s: Scene): string {
  switch (s) {
    case "projects": return "Projects";
    case "workspace": return "Workspace";
    case "process_list": return "Process List";
    case "agent_detail": return "Agent Detail";
  }
}

// ── Pure: CommandOutcome → AppAction[] ────────────────────

export function commandOutcomeToActions(
  outcome: CommandOutcome,
  scene: Scene,
  projectNames: string[],
  sel: number,
  activeProject: string | null,
): AppAction[] {
  const acts: AppAction[] = [{ type: "set_cmd_result", text: outcome.resultText }];

  if (outcome.showHelp) acts.push({ type: "push_overlay", overlay: "help" });
  if (outcome.showQuit) acts.push({ type: "push_overlay", overlay: "quitConfirm" });
  if (outcome.showCreate) acts.push({ type: "push_overlay", overlay: "createForm" });
  if (outcome.showExport) {
    const projName = projectNames[sel] ?? activeProject;
    if (projName) acts.push({ type: "push_overlay", overlay: "exportPanel", props: { project: projName } });
    else acts.push(...noticeToActions(3002));
  }
  if (outcome.showStatus) acts.push({ type: "push_overlay", overlay: "statusPanel" });
  if (outcome.showPanel) {
    acts.push({ type: "push_overlay", overlay: outcome.showPanel.overlay, props: outcome.showPanel.props });
  }
  if (outcome.refreshTrigger) acts.push({ type: "bump_refresh_key" });
  if (outcome.jumpToProject !== undefined) acts.push({ type: "select_project", index: outcome.jumpToProject });

  // Chat scenes exit command mode back to NORMAL after running.
  if (isChatScene(scene)) acts.push({ type: "exit_command" });

  // Navigation after exit_command so the navigate's mode-setting wins,
  // landing non-chat scenes (e.g. process_list) in COMMAND mode.
  if (outcome.navigateTo) acts.push({ type: "navigate", scene: outcome.navigateTo });

  return acts;
}

// ── run_command effect ────────────────────────────────────

export type RunCommandDeps = {
  dispatch: (action: AppAction) => void;
  clearCmd: () => void;
  cmdCtx: CommandContext;
  projectNames: string[];
  sel: number;
  activeProject: string | null;
};

export async function runCommandEffect(cmd: string, deps: RunCommandDeps): Promise<void> {
  const { dispatch, clearCmd, cmdCtx, projectNames, sel, activeProject } = deps;
  const scene = cmdCtx.scene;

  dispatch({ type: "push_cmd_history", cmd });

  // Bare /config /llm /lcfg open the LLM config overlay locally; sub-commands
  // (e.g. /config publish templates) fall through to executeCommand.
  const tokens = cmd.replace(/^[:\/]\s*/, "").split(/\s+/);
  const clean = tokens[0]?.toLowerCase();
  const isBareAlias = (clean === "llm" || clean === "lcfg") && tokens.length === 1;
  const isBareConfig = clean === "config" && tokens.length === 1;
  if (isBareAlias || isBareConfig) {
    const proj = projectNames[sel] ?? activeProject ?? projectNames[0] ?? null;
    dispatch({ type: "set_active_project", name: proj });
    dispatch({ type: "push_overlay", overlay: "configPanel" });
    clearCmd();
    return;
  }

  // Screen gate: reject commands not available on the current screen.
  if (clean && clean !== "quit" && clean !== "q") {
    const valid = getCommands(scene, clean).length > 0;
    if (!valid) {
      const scenes = findRootScene(clean);
      if (scenes && scenes.length > 0) {
        // Command exists but not in this scene → 1002
        for (const a of noticeToActions(1002, { scene: scenes.map(sceneLabel).join(" / ") })) dispatch(a);
        if (isChatScene(scene)) dispatch({ type: "exit_command" });
        return;
      }
      // Otherwise the command doesn't exist anywhere — fall through to
      // executeCommand, which reports 1001 (Unknown).
    }
  }

  const outcome = await executeCommand(cmd, cmdCtx);
  const acts = commandOutcomeToActions(outcome, scene, projectNames, sel, activeProject);
  for (const a of acts) dispatch(a);
  clearCmd();
}

// ── stop_process effect ───────────────────────────────────

export type StopProcessDeps = {
  dispatch: (action: AppAction) => void;
  client: McpClient;
  activeProject: string | null;
};

export async function stopProcessEffect(pid: string, deps: StopProcessDeps): Promise<void> {
  if (!deps.activeProject) return;
  try {
    const daemon = getDaemonClient();
    const caller = (daemon?.ready ? daemon : deps.client) as McpClient;
    await stopProcess(caller, deps.activeProject, pid);
    for (const a of noticeToActions(2003, { pid: pid.slice(0, 8) })) deps.dispatch(a);
  } catch (e: any) {
    for (const a of noticeToActions(2002, { reason: e.message })) deps.dispatch(a);
  }
}
