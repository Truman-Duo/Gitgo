// src/commands.ts — command execution handlers
import type { McpClient } from "./mcp/client.js";
import type { ProjectRow } from "./hooks/useGitgoData.js";
import type { Scene, OverlayType } from "./state/store.js";

import { getCommands, REGISTRY, type CommandDef } from "./keybindings.js";
export { getCommands };

import { formatNotice } from "./notices.js";

import {
  lessonVerify,
  memorySnapshot, memoryRestore,
  trialTriage,
  formalEditMessage, formalDelete, formalDissolve,
  archiveProject,
} from "./mcp/tools.js";

export type CommandContext = {
  client: McpClient;
  projects: ProjectRow[];
  sel: number;
  activeProject: string | null;
  refresh: () => Promise<void>;
  scene: Scene;
};

export type CommandOutcome = {
  resultText: string;
  jumpToProject?: number;
  showHelp?: boolean;
  showQuit?: boolean;
  showCreate?: boolean;
  showExport?: boolean;
  showStatus?: boolean;
  refreshTrigger?: boolean;
  exitApp?: boolean;
  navigateTo?: Scene;
  showPanel?: { overlay: OverlayType; props?: Record<string, any> };
};

// ── Helpers ──────────────────────────────────────────────

function noProject(): CommandOutcome {
  return { resultText: formatNotice(3001) };
}

/** Resolve the project a runtime command should act on. In chat scenes the
 *  workspace/agent's activeProject wins; elsewhere fall back to the selected row. */
function resolveProject(ctx: CommandContext): string | undefined {
  if (ctx.scene === "workspace" || ctx.scene === "agent_detail") {
    return ctx.activeProject ?? ctx.projects[ctx.sel]?.name;
  }
  return ctx.projects[ctx.sel]?.name;
}

// Normalize short root aliases (s→status, h→help, q→quit)
const SHORT_ALIASES: Record<string, string> = { s: "status", h: "help", q: "quit" };

/** Locate a root command across the full REGISTRY (ignoring scene). Returns its
 *  scene list, or undefined for global commands / unknown tokens. */
export function findRootScene(token: string): Scene[] | undefined {
  const resolved = SHORT_ALIASES[token.toLowerCase()] || token.toLowerCase();
  const node = REGISTRY.find(
    (c) => c.slashName === resolved || c.slashAliases?.includes(resolved)
  );
  return node?.scene;
}

/** Walk REGISTRY tree matching tokens against slashName + slashAliases. */
function findNode(tokens: string[], registry: CommandDef[]): { node: CommandDef; consumed: number } | null {
  let children = registry;
  let node: CommandDef | undefined;
  let consumed = 0;
  for (let i = 0; i < tokens.length; i++) {
    const match = children.find(
      (c) => c.slashName === tokens[i] || c.slashAliases?.includes(tokens[i])
    );
    if (!match) break;
    node = match;
    children = match.children || [];
    consumed = i + 1;
  }
  return node ? { node, consumed } : null;
}

// ── Shared runtime handlers ──────────────────────────────

async function lessonListHandler(ctx: CommandContext): Promise<CommandOutcome> {
  const name = resolveProject(ctx);
  if (!name) return noProject();
  return { resultText: "", showPanel: { overlay: "lessonsPanel", props: { project: name } } };
}

async function memoryListHandler(ctx: CommandContext): Promise<CommandOutcome> {
  const name = resolveProject(ctx);
  if (!name) return noProject();
  return { resultText: "", showPanel: { overlay: "memoryPanel", props: { project: name } } };
}

async function trialListHandler(ctx: CommandContext): Promise<CommandOutcome> {
  const name = resolveProject(ctx);
  if (!name) return noProject();
  return { resultText: "", showPanel: { overlay: "trialPanel", props: { project: name } } };
}

async function formalListHandler(ctx: CommandContext): Promise<CommandOutcome> {
  const name = resolveProject(ctx);
  if (!name) return noProject();
  return { resultText: "", showPanel: { overlay: "formalPanel", props: { project: name } } };
}

// ── HANDLERS: keyed by REGISTRY node name ────────────────

const HANDLERS: Record<string, (ctx: CommandContext, args: string[]) => Promise<CommandOutcome>> = {
  // System
  "app.help": async () => ({ resultText: "", showHelp: true }),
  "app.quit": async () => ({ resultText: "", showQuit: true }),

  // Projects scene
  "projects.create": async () => ({ resultText: "", showCreate: true }),
  "projects.archive": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    await archiveProject(ctx.client, name);
    ctx.refresh();
    return { resultText: `${name} archived` };
  },
  "projects.status": async () => ({ resultText: "", showStatus: true }),
  "projects.export": async () => ({ resultText: "", showExport: true }),
  "projects.config": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx) } } }),

  // Navigation
  "nav.processlist": async () => ({ resultText: "", navigateTo: "process_list" }),

  // Config deep-links (projects scene)
  "config.llm": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx) } } }),
  "config.llm.default": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx) } } }),
  "config.publish": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.templates": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.templates.list": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.templates.add": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.templates.edit": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.templates.delete": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.push": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.push.privacy": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.push.format": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.publish.push.release": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "publish" } } }),
  "config.bin": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "bin" } } }),
  "config.bin.delete_delay": async (ctx) => ({ resultText: "", showPanel: { overlay: "configPanel", props: { project: resolveProject(ctx), initialTab: "bin" } } }),

  // Runtime root — opens the secondary menu overlay
  "workspace.runtime": async () => ({
    resultText: "",
    showPanel: { overlay: "runtimeMenu" },
  }),

  // Lesson
  "runtime.lesson": lessonListHandler,
  "runtime.lesson.list": lessonListHandler,
  "runtime.lesson.search": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const q = args.join(" ");
    if (!q) return { resultText: "/runtime lesson search <query>" };
    return { resultText: "", showPanel: { overlay: "lessonsPanel", props: { project: name, initialQuery: q } } };
  },
  "runtime.lesson.verify": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const id = args[0];
    if (!id) return { resultText: "/runtime lesson verify <id>" };
    await lessonVerify(ctx.client, name, id);
    return { resultText: `Verified ${id.slice(0, 12)}` };
  },

  // Contract
  "runtime.contract": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "context", props: { project: name, initialTab: 0 } } };
  },

  // Governance
  "runtime.governance": async () => ({
    resultText: "governance <quality|patterns|feed|releases>",
  }),
  "runtime.governance.quality": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "governancePanel", props: { project: name, initialTab: 0 } } };
  },
  "runtime.governance.patterns": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "governancePanel", props: { project: name, initialTab: 1 } } };
  },
  "runtime.governance.feed": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "governancePanel", props: { project: name, initialTab: 2 } } };
  },
  "runtime.governance.releases": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "governancePanel", props: { project: name, initialTab: 3 } } };
  },

  // Memory
  "runtime.memory": memoryListHandler,
  "runtime.memory.list": memoryListHandler,
  "runtime.memory.snapshot": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    await memorySnapshot(ctx.client, name);
    return { resultText: `${name}: snapshot created` };
  },
  "runtime.memory.restore": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const ts = args[0];
    if (!ts) return { resultText: "/runtime memory restore <timestamp>" };
    await memoryRestore(ctx.client, name, ts);
    return { resultText: `${name}: restored to ${ts}` };
  },

  // History
  "runtime.history": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "context", props: { project: name, initialTab: 2 } } };
  },
  "runtime.history.full": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "context", props: { project: name, initialTab: 2 } } };
  },

  // Context
  "runtime.context": async (ctx) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    return { resultText: "", showPanel: { overlay: "context", props: { project: name, initialTab: 0 } } };
  },

  // Status (global, projects scene) — handled via showStatus above.

  // Trial
  "runtime.trial": trialListHandler,
  "runtime.trial.list": trialListHandler,
  "runtime.trial.triage": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const idx = parseInt(args[0] || "", 10);
    const act = args[1]?.toLowerCase();
    if (isNaN(idx) || !act) return { resultText: "/runtime trial triage <index> <accept|promote|discard>" };
    await trialTriage(ctx.client, name, idx, act);
    return { resultText: `${name}: triaged #${idx} → ${act}` };
  },

  // Formal
  "runtime.formal": formalListHandler,
  "runtime.formal.list": formalListHandler,
  "runtime.formal.edit": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const idx = parseInt(args[0] || "", 10);
    const msg = args.slice(1).join(" ");
    if (isNaN(idx) || !msg) return { resultText: "/runtime formal edit <index> <message>" };
    await formalEditMessage(ctx.client, name, idx, msg);
    return { resultText: `${name}: formal #${idx} edited` };
  },
  "runtime.formal.delete": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const idx = parseInt(args[0] || "", 10);
    if (isNaN(idx)) return { resultText: "/runtime formal delete <index>" };
    await formalDelete(ctx.client, name, idx);
    return { resultText: `${name}: formal #${idx} deleted` };
  },
  "runtime.formal.dissolve": async (ctx, args) => {
    const name = resolveProject(ctx);
    if (!name) return noProject();
    const idx = parseInt(args[0] || "", 10);
    if (isNaN(idx)) return { resultText: "/runtime formal dissolve <index>" };
    await formalDissolve(ctx.client, name, idx);
    return { resultText: `${name}: formal #${idx} dissolved` };
  },

};

// ── executeCommand ───────────────────────────────────────

export async function executeCommand(
  cmd: string,
  ctx: CommandContext,
): Promise<CommandOutcome> {
  let clean = cmd.trim();
  if (clean.startsWith("/")) clean = clean.slice(1).trim();
  const parts = clean.split(/\s+/);
  const action = parts[0]?.toLowerCase();
  if (!action) return { resultText: "" };

  // Normalize short root aliases (s→status, h→help, q→quit)
  const resolvedAction = SHORT_ALIASES[action] || action;
  const tokens = [resolvedAction, ...parts.slice(1)];

  // Filter REGISTRY to commands available in current scene
  const sceneRegistry = REGISTRY.filter(
    (c) => !c.scene || c.scene.includes(ctx.scene)
  );

  try {
    const found = findNode(tokens, sceneRegistry);

    if (found && HANDLERS[found.node.name]) {
      return HANDLERS[found.node.name](ctx, tokens.slice(found.consumed));
    }

    // Node partially matched — if it has children, show subcommand help
    if (found && found.node.children && found.node.children.length > 0) {
      const subs = found.node.children
        .filter((c) => !c.hidden)
        .map((c) => c.slashName)
        .join("|");
      return { resultText: `${found.node.slashName} <${subs}>` };
    }

    // Unknown command
    const avail = getCommands(ctx.scene)
      .map((c) => c.label)
      .slice(0, 10)
      .join(" / ");
    return { resultText: formatNotice(1001, { cmd: `${cmd}  —  available: ${avail}` }) };
  } catch (e: any) {
    return { resultText: formatNotice(4001, { reason: e.message }) };
  }
}
