// src/keybindings.ts — Command registry with scene + children hierarchy
// Path-aware suggestion: typing /runtime shows children, /runtime lesson shows grandchildren.
import {copy, type Copy, type Language} from "./ui/pageContract.js";
import type { Scene } from "./state/store.js";
import type { Suggestion } from "./components/CommandBar.js";
import { sortByName } from "./theme/index.js";

export type CommandDef = {
  name: string;
  title: Copy;
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

function description(def: CommandDef, language: Language) {
  return copy(def.title, language);
}

export const REGISTRY: CommandDef[] = [
  {name:"process.detail",title:{en:"Selected subprocess details",zh:"子进程详情"},category:"navigation",
    keys:[],slashName:"detail",scene:["process_list","agent_detail"]},
  {name: "process.create", title: {en:"Ask A to create and supervise a new B",zh:"请主进程创建并监督子进程"}, category: "action",
    keys: [], slashName: "create", inputMode: "fill", scene: ["process_list"]},
  {name: "process.rename", title: {en:"Rename selected B (identity unchanged)",zh:"重命名所选 B（ID 不变）"}, category: "action",
    keys: [], slashName: "rename", inputMode: "fill", scene: ["process_list"]},
  {name: "process.archive", title: {en:"Archive selected B (execution unchanged)",zh:"归档所选 B（不停止、不删除）"}, category: "action",
    keys: [], slashName: "archive", scene: ["process_list"]},
  // ═══════════════════════════════════════════════════════════
  // Global
  // ═══════════════════════════════════════════════════════════
  {
    name: "app.help",
    title: {en:"Help",zh:"帮助"},
    category: "system",
    keys: [],
    slashName: "help",
    scene: ["projects", "workspace", "process_list", "agent_detail"],
  },
  {
    name: "app.quit",
    title: {en:"Quit",zh:"退出"},
    category: "system",
    keys: ["q"],
    slashName: "quit",
  },
  {
    name: "projects.bin",
    title: {en:"Archived Projects",zh:"归档项目"},
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
    title: {en:"Create Project",zh:"创建项目"},
    category: "action",
    keys: [],
    slashName: "create",
    scene: ["projects"],
  },
  {
    name: "projects.update",
    title: {en:"Software Update",zh:"软件更新"},
    category: "system",
    keys: [],
    slashName: "update",
    scene: ["projects"],
  },
  {
    name: "projects.archive",
    title: {en:"Archive Project",zh:"归档项目"},
    category: "action",
    keys: [],
    slashName: "archive",
    scene: ["projects"],
  },
  {
    name: "projects.stats_overview",
    title: {en:"Usage Overview",zh:"全局用量概览"},
    category: "action",
    keys: [],
    slashName: "stats",
    slashAliases: ["status"],
    scene: ["projects"],
  },
  {
    name: "projects.export",
    title: {en:"Export Knowledge",zh:"导出知识"},
    category: "action",
    keys: [],
    slashName: "export",
    scene: ["projects"],
  },
  {
    name: "projects.config",
    title: {en:"Settings",zh:"设置"},
    category: "system",
    keys: [],
    slashName: "config",
    slashAliases: ["llm", "lcfg"],
    children: [
      {
        name: "config.general",
        title: {en:"General Settings",zh:"通用设置"},
        category: "system",
        keys: [],
        slashName: "general",
      },
      {
        name: "config.llm",
        title: {en:"Providers",zh:"模型服务商"},
        category: "system",
        keys: [],
        slashName: "providers",
        slashAliases: ["llm"],
        children: [
          { name: "config.llm.default", title: {en:"Default Provider",zh:"默认服务商"}, category: "system", keys: [], slashName: "default" },
        ],
      },
      {
        name: "config.bin",
        title: {en:"Bin",zh:"归档与删除"},
        category: "system",
        keys: [],
        slashName: "bin",
        hidden: true,
        children: [
          { name: "config.bin.delete_delay", title: {en:"Delete Delay",zh:"删除延迟"}, category: "system", keys: [], slashName: "delete_delay" },
        ],
      },
    ],
  },

  // ═══════════════════════════════════════════════════════════
  // Workspace scene
  // ═══════════════════════════════════════════════════════════
  {
    name: "nav.processlist",
    title: {en:"Process List",zh:"Agent 进程列表"},
    category: "navigation",
    keys: [],
    slashName: "processlist",
    scene: ["workspace", "agent_detail"],
  },
  {
    name:"workspace.publish",title:{en:"Publish",zh:"发布管理"},category:"action",keys:[],slashName:"publish",
    scene:["projects","workspace","agent_detail"],children:[
      {name:"publish.trial",title:{en:"Trial changes",zh:"试验变更"},category:"action",keys:[],slashName:"trial"},
      {name:"publish.formal",title:{en:"Formal commits",zh:"正式提交"},category:"action",keys:[],slashName:"formal"},
    ],
  },
  {
    name: "workspace.compact",
    title: {en:"Compact Context",zh:"压缩上下文"},
    category: "action",
    keys: [],
    slashName: "compact",
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.undo",
    title: {en:"Rewind Latest Conversation Turn",zh:"撤回最近一轮会话"},
    category: "action",
    keys: [],
    slashName: "undo",
    slashAliases: ["rewind"],
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.btw",
    title: {en:"Isolated Side Question",zh:"独立旁路问答"},
    category: "action",
    keys: [],
    slashName: "btw",
    inputMode: "fill",
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.stats",
    title: {en:"Project Statistics & Timeline",zh:"项目统计与任务时间轴"},
    category: "action",
    keys: [],
    slashName: "stats",
    slashAliases: ["status"],
    scene: ["workspace", "agent_detail"],
  },
  {
    name: "workspace.runtime",
    title: {en:"Runtime Data",zh:"运行时数据"},
    category: "action",
    keys: [],
    slashName: "runtime",
    scene: ["workspace", "agent_detail"],
    children: [
      {
        name: "runtime.lesson",
        title: {en:"Lessons",zh:"经验知识"},
        category: "action",
        keys: [],
        slashName: "lesson",
        children: [
          { name: "runtime.lesson.list", title: {en:"List lessons",zh:"浏览经验"}, category: "action", keys: [], slashName: "list" },
          { name: "runtime.lesson.search", title: {en:"Search lessons",zh:"搜索经验"}, category: "action", keys: [], slashName: "search" },
          { name: "runtime.lesson.verify", title: {en:"Verify a lesson by ID",zh:"按 ID 验证经验"}, category: "action", keys: [], slashName: "verify" },
        ],
      },
      {
        name: "runtime.contract",
        title: {en:"Contract",zh:"项目合同"},
        category: "action",
        keys: [],
        slashName: "contract",
      },
      {
        name: "runtime.governance",
        title: {en:"Governance",zh:"治理数据"},
        category: "action",
        keys: [],
        slashName: "governance",
        children: [
          { name: "runtime.governance.quality", title: {en:"Quality Metrics",zh:"质量指标"}, category: "action", keys: [], slashName: "quality" },
          { name: "runtime.governance.patterns", title: {en:"Change Patterns",zh:"变更模式"}, category: "action", keys: [], slashName: "patterns" },
          { name: "runtime.governance.feed", title: {en:"Event Feed",zh:"事件记录"}, category: "action", keys: [], slashName: "feed" },
          { name: "runtime.governance.releases", title: {en:"Releases",zh:"发布记录"}, category: "action", keys: [], slashName: "releases" },
        ],
      },
      {
        name: "runtime.memory",
        title: {en:"Memory Snapshots",zh:"记忆快照"},
        category: "action",
        keys: [],
        slashName: "memory",
        children: [
          { name: "runtime.memory.snapshot", title: {en:"Create snapshot",zh:"创建快照"}, category: "action", keys: [], slashName: "snapshot" },
          { name: "runtime.memory.list", title: {en:"List snapshots",zh:"浏览快照"}, category: "action", keys: [], slashName: "list" },
          { name: "runtime.memory.restore", title: {en:"Restore snapshot by timestamp",zh:"按时间恢复快照"}, category: "action", keys: [], slashName: "restore" },
        ],
      },
      {
        name: "runtime.context",
        title: {en:"Context Panel",zh:"上下文面板"},
        category: "navigation",
        keys: [],
        slashName: "context",
      },
      {
        name: "runtime.tools",
        title: {en:"Saved Custom Tools",zh:"自定义动作"},
        category: "action",
        keys: [],
        slashName: "tools",
      },
      {
        name: "runtime.recovery",
        title: {en:"Daemon Recovery",zh:"Daemon 恢复"},
        category: "action",
        keys: [],
        slashName: "recovery",
        children: [
          { name: "runtime.recovery.resume", title: {en:"Resume a safe candidate",zh:"恢复安全候选"}, category: "action", keys: [], slashName: "resume" },
          { name: "runtime.recovery.resume_verified", title: {en:"Resume after manual verification",zh:"人工核验后恢复"}, category: "action", keys: [], slashName: "resume_verified" },
          { name: "runtime.recovery.discard", title: {en:"Discard a recovery candidate",zh:"放弃恢复候选"}, category: "action", keys: [], slashName: "discard" },
        ],
      },
      {
        name: "runtime.trial",
        title: {en:"Trial (External PRs)",zh:"外部 PR 试验仓"},
        category: "action",
        keys: [],
        slashName: "trial",
        hidden: true,
        children: [
          { name: "runtime.trial.list", title: {en:"List incoming PRs",zh:"浏览外部 PR"}, category: "action", keys: [], slashName: "list" },
          { name: "runtime.trial.triage", title: {en:"Triage: accept/promote/discard",zh:"审阅并处置试验"}, category: "action", keys: [], slashName: "triage" },
        ],
      },
      {
        name: "runtime.formal",
        title: {en:"Formal Commits",zh:"正式提交"},
        category: "action",
        keys: [],
        slashName: "formal",
        hidden: true,
        children: [
          { name: "runtime.formal.list", title: {en:"List formal commits",zh:"浏览正式提交"}, category: "action", keys: [], slashName: "list" },
          { name: "runtime.formal.edit", title: {en:"Edit commit message",zh:"编辑提交说明"}, category: "action", keys: [], slashName: "edit" },
          { name: "runtime.formal.delete", title: {en:"Delete formal commit",zh:"删除正式提交"}, category: "action", keys: [], slashName: "delete" },
          { name: "runtime.formal.dissolve", title: {en:"Dissolve formal commit",zh:"解除正式提交"}, category: "action", keys: [], slashName: "dissolve" },
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
    c.scene?.includes(scene) && c.name.startsWith("process.") || c.name === "app.help"
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
export function getKeybindings(scene: Scene, language: Language = "en"): Array<Omit<CommandDef,"title"> & {title:string}> {
  return sortByName(topLevelCommands(scene).map(def=>({...def,title:description(def,language)})));
}
