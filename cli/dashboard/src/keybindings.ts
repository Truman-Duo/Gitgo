// src/keybindings.ts — Command registry with scene + children hierarchy
// Path-aware suggestion: typing /runtime shows children, /runtime lesson shows grandchildren.
import type { Scene } from "./state/store.js";
import type { Suggestion } from "./components/CommandBar.js";
import { sortByName } from "./theme/index.js";

export type CommandDef = {
  name: string;
  title: string;
  category: "system" | "navigation" | "action";
  keys: string[];
  slashName: string;
  slashAliases?: string[];
  scene?: Scene[];
  hidden?: boolean;
  /** Parameterised commands fill the editor first; Enter must not execute an empty invocation. */
  inputMode?: "execute" | "fill";
  children?: CommandDef[];
};

const ZH_TITLES: Record<string, string> = {
  "app.help": "帮助", "app.quit": "退出", "projects.bin": "归档项目",
  "projects.create": "创建项目", "projects.archive": "归档项目",
  "projects.stats_overview": "全局用量概览", "workspace.stats": "项目统计与任务时间轴", "projects.export": "导出知识",
  "projects.config": "设置", "config.general": "通用设置",
  "config.llm": "模型服务商", "config.publish": "发布设置",
  "nav.processlist": "Agent 进程列表", "workspace.compact": "压缩上下文",
  "workspace.undo": "撤回最近一轮会话",
  "workspace.btw": "独立旁路问答", "workspace.runtime": "运行时数据",
  "runtime.lesson": "经验知识", "runtime.contract": "项目合同",
  "runtime.governance": "治理数据", "runtime.memory": "记忆快照",
  "runtime.history": "操作历史", "runtime.context": "上下文面板",
  "runtime.recovery": "Daemon 恢复", "runtime.trial": "外部 PR 试验仓",
  "runtime.formal": "正式提交",
  "process.rename": "重命名所选 B（ID 不变）", "process.archive": "归档所选 B（不停止、不删除）",
};

function description(def: CommandDef, language: "en" | "zh") {
  return language === "zh" ? (ZH_TITLES[def.name] || def.title) : def.title;
}

export const REGISTRY: CommandDef[] = [
  {name: "process.create", title: "Ask A to create and supervise a new B", category: "action",
    keys: [], slashName: "create", inputMode: "fill", scene: ["process_list"]},
  {name: "process.rename", title: "Rename selected B (identity unchanged)", category: "action",
    keys: [], slashName: "rename", inputMode: "fill", scene: ["process_list"]},
  {name: "process.archive", title: "Archive selected B (execution unchanged)", category: "action",
    keys: [], slashName: "archive", scene: ["process_list"]},
  // ═══════════════════════════════════════════════════════════
  // Global
  // ═══════════════════════════════════════════════════════════
  {
    name: "app.help",
    title: "Help",
    category: "system",
    keys: [],
    slashName: "help",
    scene: ["projects", "workspace", "process_list", "agent_detail"],
  },
  {
    name: "app.quit",
    title: "Quit",
    category: "system",
    keys: ["q"],
    slashName: "quit",
  },
  {
    name: "projects.bin",
    title: "Archived Projects",
    category: "action",
    keys: [],
    slashName: "bin",
    scene: ["projects"],
  },

  // ═══════════════════════════════════════════════════════════
  // Projects scene
  // ═══════════════════════════════════════════════════════════
  {
    name: "projects.create",
    title: "Create Project",
    category: "action",
    keys: [],
    slashName: "create",
    scene: ["projects"],
  },
  {
    name: "projects.archive",
    title: "Archive Project",
    category: "action",
    keys: [],
    slashName: "archive",
    scene: ["projects"],
  },
  {
    name: "projects.stats_overview",
    title: "Usage Overview",
    category: "action",
    keys: [],
    slashName: "stats",
    slashAliases: ["status"],
    scene: ["projects"],
  },
  {
    name: "projects.export",
    title: "Export Knowledge",
    category: "action",
    keys: [],
    slashName: "export",
    scene: ["projects"],
  },
  {
    name: "projects.config",
    title: "Settings",
    category: "system",
    keys: [],
    slashName: "config",
    slashAliases: ["llm", "lcfg"],
    children: [
      {
        name: "config.general",
        title: "General Settings",
        category: "system",
        keys: [],
        slashName: "general",
      },
      {
        name: "config.llm",
        title: "Providers",
        category: "system",
        keys: [],
        slashName: "providers",
        slashAliases: ["llm"],
        children: [
          { name: "config.llm.default", title: "Default Provider", category: "system", keys: [], slashName: "default" },
        ],
      },
      {
        name: "config.bin",
        title: "Bin",
        category: "system",
        keys: [],
        slashName: "bin",
        hidden: true,
        children: [
          { name: "config.bin.delete_delay", title: "Delete Delay", category: "system", keys: [], slashName: "delete_delay" },
        ],
      },
    ],
  },

  // ═══════════════════════════════════════════════════════════
  // Workspace scene
  // ═══════════════════════════════════════════════════════════
  {
    name: "nav.processlist",
    title: "Process List",
    category: "navigation",
    keys: [],
    slashName: "processlist",
    scene: ["workspace", "agent_detail"],
  },
  {
    name:"workspace.publish",title:"Publish",category:"action",keys:[],slashName:"publish",
    scene:["projects","workspace","agent_detail"],children:[
      {name:"publish.trial",title:"Trial changes",category:"action",keys:[],slashName:"trial"},
      {name:"publish.formal",title:"Formal commits",category:"action",keys:[],slashName:"formal"},
    ],
  },
  {
    name: "workspace.compact",
    title: "Compact Context",
    category: "action",
    keys: [],
    slashName: "compact",
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.undo",
    title: "Rewind Latest Conversation Turn",
    category: "action",
    keys: [],
    slashName: "undo",
    slashAliases: ["rewind"],
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.btw",
    title: "Isolated Side Question",
    category: "action",
    keys: [],
    slashName: "btw",
    inputMode: "fill",
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.stats",
    title: "Project Statistics & Timeline",
    category: "action",
    keys: [],
    slashName: "stats",
    slashAliases: ["status"],
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.runtime",
    title: "Runtime Data",
    category: "action",
    keys: [],
    slashName: "runtime",
    scene: ["workspace", "agent_detail"],
    children: [
      {
        name: "runtime.lesson",
        title: "Lessons",
        category: "action",
        keys: [],
        slashName: "lesson",
        children: [
          { name: "runtime.lesson.list", title: "List lessons", category: "action", keys: [], slashName: "list" },
          { name: "runtime.lesson.search", title: "Search lessons", category: "action", keys: [], slashName: "search" },
          { name: "runtime.lesson.verify", title: "Verify a lesson by ID", category: "action", keys: [], slashName: "verify" },
        ],
      },
      {
        name: "runtime.contract",
        title: "Contract",
        category: "action",
        keys: [],
        slashName: "contract",
      },
      {
        name: "runtime.governance",
        title: "Governance",
        category: "action",
        keys: [],
        slashName: "governance",
        children: [
          { name: "runtime.governance.quality", title: "Quality Metrics", category: "action", keys: [], slashName: "quality" },
          { name: "runtime.governance.patterns", title: "Change Patterns", category: "action", keys: [], slashName: "patterns" },
          { name: "runtime.governance.feed", title: "Event Feed", category: "action", keys: [], slashName: "feed" },
          { name: "runtime.governance.releases", title: "Releases", category: "action", keys: [], slashName: "releases" },
        ],
      },
      {
        name: "runtime.memory",
        title: "Memory Snapshots",
        category: "action",
        keys: [],
        slashName: "memory",
        children: [
          { name: "runtime.memory.snapshot", title: "Create snapshot", category: "action", keys: [], slashName: "snapshot" },
          { name: "runtime.memory.list", title: "List snapshots", category: "action", keys: [], slashName: "list" },
          { name: "runtime.memory.restore", title: "Restore snapshot by timestamp", category: "action", keys: [], slashName: "restore" },
        ],
      },
      {
        name: "runtime.context",
        title: "Context Panel",
        category: "navigation",
        keys: [],
        slashName: "context",
      },
      {
        name: "runtime.tools",
        title: "Saved Custom Tools",
        category: "action",
        keys: [],
        slashName: "tools",
      },
      {
        name: "runtime.recovery",
        title: "Daemon Recovery",
        category: "action",
        keys: [],
        slashName: "recovery",
        children: [
          { name: "runtime.recovery.resume", title: "Resume a safe candidate", category: "action", keys: [], slashName: "resume" },
          { name: "runtime.recovery.resume_verified", title: "Resume after manual verification", category: "action", keys: [], slashName: "resume_verified" },
          { name: "runtime.recovery.discard", title: "Discard a recovery candidate", category: "action", keys: [], slashName: "discard" },
        ],
      },
      {
        name: "runtime.trial",
        title: "Trial (External PRs)",
        category: "action",
        keys: [],
        slashName: "trial",
        hidden: true,
        children: [
          { name: "runtime.trial.list", title: "List incoming PRs", category: "action", keys: [], slashName: "list" },
          { name: "runtime.trial.triage", title: "Triage: accept/promote/discard", category: "action", keys: [], slashName: "triage" },
        ],
      },
      {
        name: "runtime.formal",
        title: "Formal Commits",
        category: "action",
        keys: [],
        slashName: "formal",
        hidden: true,
        children: [
          { name: "runtime.formal.list", title: "List formal commits", category: "action", keys: [], slashName: "list" },
          { name: "runtime.formal.edit", title: "Edit commit message", category: "action", keys: [], slashName: "edit" },
          { name: "runtime.formal.delete", title: "Delete formal commit", category: "action", keys: [], slashName: "delete" },
          { name: "runtime.formal.dissolve", title: "Dissolve formal commit", category: "action", keys: [], slashName: "dissolve" },
        ],
      },
    ],
  },

  // ═══════════════════════════════════════════════════════════
  // (agent_detail scene reuses workspace.runtime above)
  // ═══════════════════════════════════════════════════════════
];

// ── Path-aware suggestion ──────────────────────────────────

/** Walk the registry tree following input tokens. Returns the deepest matching node and remaining path. */
function resolvePath(input: string, registry: CommandDef[]): { node: CommandDef | null; children: CommandDef[] } {
  const tokens = input.trim().split(/\s+/);
  if (tokens.length === 0 || tokens[0] === "") return { node: null, children: visible(registry) };

  // Find root command (first token)
  const root = registry.find((c) =>
    c.slashName === tokens[0] || c.slashAliases?.includes(tokens[0])
  );
  // A complete but unavailable root command must not fall back to unrelated
  // commands from the current scene. Partial top-level input is handled by
  // getCommands() before this resolver is called.
  if (!root) return { node: null, children: [] };

  let current: CommandDef = root;
  let currentChildren = visible(current.children || []);
  for (let i = 1; i < tokens.length; i++) {
    const child = currentChildren.find((c) =>
      c.slashName === tokens[i] || c.slashAliases?.includes(tokens[i])
    );
    if (!child) {
      // No exact child match — if input ends with a partial (e.g. "les" for "lesson"), filter
      const partial = tokens[i].toLowerCase();
      const matching = currentChildren.filter((c) => c.slashName.startsWith(partial));
      return { node: current, children: matching.length > 0 ? matching : currentChildren };
    }
    current = child;
    currentChildren = visible(current.children || []);
  }

  return { node: current, children: currentChildren };
}

function visible(defs: CommandDef[]): CommandDef[] {
  return defs.filter((c) => !c.hidden);
}

/** Return top-level suggestions for the given scene. */
function topLevelCommands(scene: Scene): CommandDef[] {
  if (scene === "process_list") return visible(REGISTRY).filter(c =>
    c.name.startsWith("process.") || c.name === "app.help"
  );
  return visible(REGISTRY).filter((c) => !c.scene || c.scene.includes(scene));
}

/** Get suggestions for the current command input and scene. */
export function getCommands(scene: Scene): Suggestion[];
export function getCommands(scene: Scene, cmdValue: string): Suggestion[];
export function getCommands(scene: Scene, cmdValue: string | undefined, language: "en" | "zh"): Suggestion[];
export function getCommands(scene: Scene, cmdValue?: string, language: "en" | "zh" = "en"): Suggestion[] {
  const top = topLevelCommands(scene);

  if (!cmdValue || cmdValue.trim().length === 0) {
    return sortByName(top.map((c) => ({ label: "/" + c.slashName, description: description(c, language), inputMode: c.inputMode })));
  }

  // Strip leading slash if present, then parse path
  const input = cmdValue.startsWith("/") ? cmdValue.slice(1) : cmdValue;
  const tokens = input.trim().split(/\s+/);

  if (tokens.length === 0) {
    return sortByName(top.map((c) => ({ label: "/" + c.slashName, description: description(c, language), inputMode: c.inputMode })));
  }

  const firstToken = tokens[0].toLowerCase();

  // If only the first token is partially typed, filter top-level
  if (tokens.length === 1 && !input.endsWith(" ")) {
    return sortByName(
      top
        .filter((c) => c.slashName.startsWith(firstToken) || c.slashAliases?.some((a) => a.startsWith(firstToken)))
        .map((c) => ({ label: "/" + c.slashName, description: description(c, language), inputMode: c.inputMode })),
    );
  }

  // Walk the path. If input ends with space, all tokens are complete — resolve full path.
  // Otherwise the last token is partial — resolve the parent path, then filter below.
  const pathToResolve = input.endsWith(" ")
    ? tokens.join(" ")
    : tokens.slice(0, -1).join(" ");
  const { children } = resolvePath(pathToResolve, top);

  // If the last token is "in-progress" (no trailing space), filter children
  const lastToken = tokens[tokens.length - 1].toLowerCase();
  if (!input.endsWith(" ") && tokens.length > 1) {
    return sortByName(
      children
        .filter((c) => c.slashName.startsWith(lastToken))
        .map((c) => ({ label: c.slashName, description: description(c, language), inputMode: c.inputMode })),
    );
  }

  // Full path resolved — show all children
  return sortByName(children.map((c) => ({ label: c.slashName, description: description(c, language), inputMode: c.inputMode })));
}

/** Returns all keybindings visible in the help panel for a given scene */
export function getKeybindings(scene: Scene): CommandDef[] {
  const defs = visible(REGISTRY).filter((c) => !c.scene || c.scene.includes(scene));
  return sortByName(defs);
}
