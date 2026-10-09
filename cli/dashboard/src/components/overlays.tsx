// src/components/overlays.tsx — overlay → panel 渲染映射。
// 每个 overlay 一个 case。加一个新 overlay panel = 在 store 的 OverlayType 加
// 一个枚举值 + 在此 switch 加一个 case，其余不变。
import React from "react";
import type { BackendClient } from "../backend/client.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import type { ProcessInfo, ToolEvent } from "../hooks/useLoopData.js";
import { runCommandEffect, type RunCommandDeps } from "../effects/run.js";
import type { Scene, AppState, AppAction } from "../state/store.js";
import type { FooterConfig } from "./CommandBar.js";
import type { DialogItem } from "./DialogSelect.js";
import { HelpPanel } from "./HelpPanel.js";
import { QuitPanel } from "./QuitPanel.js";
import { InlineContext } from "./InlineContext.js";
import { ConfigPanel } from "./ConfigPanel.js";
import { BinPanel } from "./BinPanel.js";
import { PublishPanel } from "./PublishPanel.js";
import { CreateProjectPanel } from "./CreateProjectPanel.js";
import { StatsPanel } from "./StatsPanel.js";
import { StatsOverview } from "./StatsOverview.js";
import { BtwPanel } from "./BtwPanel.js";
import { CompactPanel } from "./CompactPanel.js";
import { InterruptPanel } from "./InterruptPanel.js";
import { UndoPanel } from "./UndoPanel.js";
import { ExportPanel } from "./ExportPanel.js";
import { GovernancePanel } from "./GovernancePanel.js";
import { MemoryPanel } from "./MemoryPanel.js";
import { TrialPanel } from "./TrialPanel.js";
import { FormalPanel } from "./FormalPanel.js";
import { LessonsPanel } from "./LessonsPanel.js";
import { RuntimeMenu } from "./RuntimeMenu.js";
import { RuntimeContextPanel } from "./RuntimeContextPanel.js";
import { RuntimeToolsPanel } from "./RuntimeToolsPanel.js";
import { RuntimeChildShell } from "./RuntimeShell.js";
import { DialogSelect } from "./DialogSelect.js";
import { getCommands } from "../commands.js";
import { getKeybindings } from "../keybindings.js";

export interface OverlayCtx {
  client: BackendClient;
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
  toolEvents: ToolEvent[];
  processes: Record<string, ProcessInfo>;
  sendChat: (text: string) => void;
  verbose: boolean;
  onSettingSaved?: (key: string) => Promise<string | void>;
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
    case "undoPanel":
      return <UndoPanel client={ctx.client}
        project={overlay.props?.project || ctx.activeProject || ""}
        processId={overlay.props?.processId || ""} preview={overlay.props?.preview || {}}
        onDismiss={ctx.popOverlay}
        onCommitted={() => { ctx.dispatch({type: "bump_refresh_key"}); ctx.refresh(); }} />;
    case "interruptConfirm":
      return <InterruptPanel client={ctx.client} project={overlay.props?.project || ctx.activeProject || ""}
        processId={overlay.props?.processId || ""} requestId={overlay.props?.requestId || ""}
        subtree={!!overlay.props?.subtree} onDismiss={ctx.popOverlay} />;
    case "compactPanel":
      return <CompactPanel client={ctx.client}
        project={overlay.props?.project || ctx.activeProject || ""}
        processId={overlay.props?.processId || ""} result={overlay.props?.result}
        onDismiss={ctx.popOverlay} />;
    case "help":
      return <HelpPanel scene={ctx.scene} onDismiss={ctx.popOverlay} />;
    case "quitConfirm":
      return (
        <QuitPanel
          onQuit={() => {
            ctx.popOverlay();
            ctx.exit();
          }}
          onCancel={ctx.popOverlay}
        />
      );
    case "context": {
      const ctxProject = overlay.props?.project || ctx.activeProject;
      const panel = ctxProject ? (
        <InlineContext project={ctxProject} client={ctx.client} cols={ctx.w}
          toolEvents={ctx.toolEvents} initialTab={overlay.props?.initialTab ?? 0} onDismiss={ctx.popOverlay} />
      ) : null;
      return panel && overlay.props?.runtimeTab
        ? <RuntimeChildShell tab="contract">{panel}</RuntimeChildShell>
        : panel;
    }
    case "configPanel": {
      const llmProject = overlay.props?.project || ctx.activeProject || "";
      return (
        <ConfigPanel client={ctx.client} project={llmProject}
          initialTab={overlay.props?.initialTab ?? "general"}
          initialSetting={overlay.props?.initialSetting}
          onSettingSaved={ctx.onSettingSaved}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
          onBack={ctx.popOverlay}
          onStatusUpdate={ctx.setScreenStatusText}
          onRefresh={ctx.refresh} />
      );
    }
    case "runtimeContext": {
      const runtimeProject=overlay.props?.project||ctx.activeProject;
      return runtimeProject?<RuntimeChildShell tab="context"><RuntimeContextPanel project={runtimeProject} processes={ctx.processes} onDismiss={ctx.popOverlay}/></RuntimeChildShell>:null;
    }
    case "binPanel":
      return (
        <BinPanel client={ctx.client} cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride} onDismiss={ctx.popOverlay}
          onRefresh={ctx.refresh} />
      );
    case "publishPanel": {
      const publishProject=overlay.props?.project||ctx.activeProject;
      return publishProject?<PublishPanel client={ctx.client} project={publishProject}
        cmdInput={ctx.llmCmdInput} onFooter={ctx.setFooterOverride} onDismiss={ctx.popOverlay}
        onOpenTrial={()=>ctx.dispatch({type:"push_overlay",overlay:"trialPanel",props:{project:publishProject}})}
        onOpenFormal={()=>ctx.dispatch({type:"push_overlay",overlay:"formalPanel",props:{project:publishProject}})}/>:null;
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
          defaultWorkspace=""
          onDismiss={ctx.popOverlay}
          onCreated={() => { ctx.refresh(); ctx.popOverlay(); }}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
        />
      );
    case "statsPanel": {
      const project = overlay.props?.project || ctx.activeProject;
      return project ? <StatsPanel client={ctx.client} project={project}
        cols={ctx.w} onDismiss={ctx.popOverlay} /> : null;
    }
    case "statsOverview":
      return <StatsOverview client={ctx.client} cols={ctx.w} onDismiss={ctx.popOverlay} />;
    case "btwPanel":
      return <BtwPanel client={ctx.client}
        project={String(overlay.props?.project || ctx.activeProject || "")}
        processId={String(overlay.props?.processId || "")}
        question={String(overlay.props?.question || "")}
        answer={String(overlay.props?.answer || "")}
        reasoning={String(overlay.props?.reasoning || "")}
        sidecarId={String(overlay.props?.sidecarId || "")}
        processes={ctx.processes}
        verbose={ctx.verbose}
        onDismiss={ctx.popOverlay} onApply={ctx.sendChat} />;
    case "exportPanel": {
      const exportProject = overlay.props?.project || ctx.activeProject;
      return exportProject ? (
        <ExportPanel
          client={ctx.client}
          project={exportProject}
          cols={ctx.w}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
          onDismiss={ctx.popOverlay}
        />
      ) : null;
    }
    case "governancePanel": {
      const govProject = overlay.props?.project || ctx.activeProject;
      return govProject ? (
        <RuntimeChildShell tab="governance"><GovernancePanel
          client={ctx.client}
          project={govProject}
          cols={ctx.w}
          initialTab={overlay.props?.initialTab ?? 0}
          onDismiss={ctx.popOverlay}
        /></RuntimeChildShell>
      ) : null;
    }
    case "memoryPanel": {
      const memProject = overlay.props?.project || ctx.activeProject;
      return memProject ? (
        <RuntimeChildShell tab="memory"><MemoryPanel client={ctx.client} project={memProject} cols={ctx.w} onDismiss={ctx.popOverlay} /></RuntimeChildShell>
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
        <RuntimeChildShell tab="lesson"><LessonsPanel client={ctx.client} project={lessonsProject} cols={ctx.w}
          initialQuery={overlay.props?.initialQuery} onDismiss={ctx.popOverlay}
          cmdInput={ctx.llmCmdInput} onFooter={ctx.setFooterOverride} /></RuntimeChildShell>
      ) : null;
    }
    case "runtimeToolsPanel": {
      const toolsProject = overlay.props?.project || ctx.activeProject;
      return toolsProject ? (
        <RuntimeChildShell tab="tools"><RuntimeToolsPanel client={ctx.client} project={toolsProject} cols={ctx.w}
          onDismiss={ctx.popOverlay} /></RuntimeChildShell>
      ) : null;
    }
    case "runtimeMenu":
      return (
        <RuntimeMenu
          cols={ctx.w}
          rows={ctx.h}
          client={ctx.client}
          project={String(overlay.props?.project || ctx.activeProject || "")}
          processes={ctx.processes}
          cmdInput={ctx.llmCmdInput}
          onFooter={ctx.setFooterOverride}
          onSelect={(sub) => {
            // Keep the runtime root below its child. Esc from lesson/context/
            // tools returns one level to the tab shell; a second Esc returns
            // to the conversation.
            void ctx.runCommandEffect("/runtime " + sub, ctx.runCommandDeps);
          }}
          onDismiss={ctx.popOverlay}
        />
      );
    default:
      return null;
  }
}
