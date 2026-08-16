// src/components/overlays.tsx — overlay → panel 渲染映射。
// 每个 overlay 一个 case。加一个新 overlay panel = 在 store 的 OverlayType 加
// 一个枚举值 + 在此 switch 加一个 case，其余不变。
import React from "react";
import type { McpClient } from "../mcp/client.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import { runCommandEffect, type RunCommandDeps } from "../effects/run.js";
import type { Scene, AppState, AppAction } from "../state/store.js";
import type { FooterConfig } from "./CommandBar.js";
import type { DialogItem } from "./DialogSelect.js";
import { HelpPanel } from "./HelpPanel.js";
import { QuitPanel } from "./QuitPanel.js";
import { InlineContext } from "./InlineContext.js";
import { ConfigPanel } from "./ConfigPanel.js";
import { CreateProjectPanel } from "./CreateProjectPanel.js";
import { StatusPanel } from "./StatusPanel.js";
import { ExportPanel } from "./ExportPanel.js";
import { GovernancePanel } from "./GovernancePanel.js";
import { MemoryPanel } from "./MemoryPanel.js";
import { TrialPanel } from "./TrialPanel.js";
import { FormalPanel } from "./FormalPanel.js";
import { LessonsPanel } from "./LessonsPanel.js";
import { RuntimeMenu } from "./RuntimeMenu.js";
import { DialogSelect } from "./DialogSelect.js";
import { getCommands } from "../commands.js";
import { getKeybindings } from "../keybindings.js";

export interface OverlayCtx {
  client: McpClient;
  scene: Scene;
  activeProject: string | null;
  w: number;
  h: number;
  llmCmdInput: UseTextInputReturn;
  cmdInput: UseTextInputReturn;
  dispatch: (action: AppAction) => void;
  popOverlay: () => void;
  refresh: () => void;
  navigate: (
    scene: Scene,
    patch?: Partial<Pick<AppState, "activeProject" | "activeAgentId" | "processListSelIdx">>,
  ) => void;
  setFooterOverride: (cfg: FooterConfig | null) => void;
  setScreenStatusText: (text: string) => void;
  exit: () => void;
  projects: ProjectRow[];
  runCommandEffect: (cmd: string, deps: RunCommandDeps) => Promise<void>;
  runCommandDeps: RunCommandDeps;
}

function enterCommandMode(
  cmdInput: UseTextInputReturn,
  dispatch: (action: AppAction) => void,
) {
  cmdInput.setValue("");
  dispatch({ type: "enter_command" });
}

export function renderOverlay(
  overlay: { type: string; props?: Record<string, any> },
  ctx: OverlayCtx,
) {
  switch (overlay.type) {
    case "help":
      return <HelpPanel scene={ctx.scene} onDismiss={ctx.popOverlay} />;
    case "quitConfirm":
      return (
        <QuitPanel
          onSaveAndQuit={() => {
            // Notify daemon, then exit gracefully
            ctx.popOverlay();
            ctx.exit();
          }}
          onForceQuit={() => {
            ctx.popOverlay();
            ctx.exit();
          }}
          onCancel={ctx.popOverlay}
        />
      );
    case "context": {
      const ctxProject = overlay.props?.project || ctx.activeProject;
      return ctxProject ? (
        <InlineContext project={ctxProject} client={ctx.client} cols={ctx.w}
          toolEvents={[]} initialTab={overlay.props?.initialTab ?? 0} onDismiss={ctx.popOverlay} />
      ) : null;
    }
    case "configPanel": {
      const llmProject = overlay.props?.project || ctx.activeProject || "";
      return (
        <ConfigPanel client={ctx.client} project={llmProject}
          initialTab={overlay.props?.initialTab ?? "providers"}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
          onBack={ctx.popOverlay}
          onStatusUpdate={ctx.setScreenStatusText}
          onRefresh={ctx.refresh} />
      );
    }
    case "whichkey": {
      const bindings = getKeybindings(ctx.scene);
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
            ctx.popOverlay();
            const def = bindings.find((c) => c.name === id);
            if (def) {
              enterCommandMode(ctx.cmdInput, ctx.dispatch);
              ctx.cmdInput.setValue("/" + def.slashName);
            }
          }}
          onDismiss={ctx.popOverlay}
          title="Which Key?"
          placeholder="Filter commands..."
          height={ctx.h}
        />
      );
    }
    case "dialogSelect": {
      const cmds = getCommands(ctx.scene);
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
            ctx.popOverlay();
            enterCommandMode(ctx.cmdInput, ctx.dispatch);
            ctx.cmdInput.setValue(id);
          }}
          onDismiss={ctx.popOverlay}
          title="Command Palette"
          placeholder="Type command..."
          height={ctx.h}
        />
      );
    }
    case "createForm":
      return (
        <CreateProjectPanel
          client={ctx.client}
          defaultWorkspace={process.cwd()}
          onDismiss={ctx.popOverlay}
          onCreated={() => { ctx.refresh(); ctx.popOverlay(); }}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
        />
      );
    case "statusPanel":
      return (
        <StatusPanel
          projects={ctx.projects}
          cols={ctx.w}
          onDismiss={ctx.popOverlay}
          onEnterProject={(name) => {
            ctx.popOverlay();
            ctx.navigate("workspace", { activeProject: name, activeAgentId: null });
          }}
        />
      );
    case "exportPanel": {
      const exportProject = overlay.props?.project || ctx.activeProject;
      return exportProject ? (
        <ExportPanel
          client={ctx.client}
          project={exportProject}
          cols={ctx.w}
          onDismiss={ctx.popOverlay}
        />
      ) : null;
    }
    case "governancePanel": {
      const govProject = overlay.props?.project || ctx.activeProject;
      return govProject ? (
        <GovernancePanel
          client={ctx.client}
          project={govProject}
          cols={ctx.w}
          initialTab={overlay.props?.initialTab ?? 0}
          onDismiss={ctx.popOverlay}
        />
      ) : null;
    }
    case "memoryPanel": {
      const memProject = overlay.props?.project || ctx.activeProject;
      return memProject ? (
        <MemoryPanel client={ctx.client} project={memProject} cols={ctx.w} onDismiss={ctx.popOverlay} />
      ) : null;
    }
    case "trialPanel": {
      const trialProject = overlay.props?.project || ctx.activeProject;
      return trialProject ? (
        <TrialPanel client={ctx.client} project={trialProject} cols={ctx.w} onDismiss={ctx.popOverlay} />
      ) : null;
    }
    case "formalPanel": {
      const formalProject = overlay.props?.project || ctx.activeProject;
      return formalProject ? (
        <FormalPanel client={ctx.client} project={formalProject} cols={ctx.w} onDismiss={ctx.popOverlay} />
      ) : null;
    }
    case "lessonsPanel": {
      const lessonsProject = overlay.props?.project || ctx.activeProject;
      return lessonsProject ? (
        <LessonsPanel client={ctx.client} project={lessonsProject} cols={ctx.w}
          initialQuery={overlay.props?.initialQuery} onDismiss={ctx.popOverlay} />
      ) : null;
    }
    case "runtimeMenu":
      return (
        <RuntimeMenu
          cols={ctx.w}
          rows={ctx.h}
          onSelect={(sub) => {
            ctx.popOverlay();
            void ctx.runCommandEffect("/runtime " + sub, ctx.runCommandDeps);
          }}
          onDismiss={ctx.popOverlay}
        />
      );
    default:
      return null;
  }
}
