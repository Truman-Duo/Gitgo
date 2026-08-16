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
  children?: CommandDef[];
};

export const REGISTRY: CommandDef[] = [
  // ═══════════════════════════════════════════════════════════
  // Global
  // ═══════════════════════════════════════════════════════════
  {
    name: "app.help",
    title: "Help",
    category: "system",
    keys: ["?"],
    slashName: "help",
  },
  {
    name: "app.quit",
    title: "Quit",
    category: "system",
    keys: ["q"],
    slashName: "quit",
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
    name: "projects.status",
    title: "Global Status",
    category: "action",
    keys: [],
    slashName: "status",
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
    scene: ["projects"],
    children: [
      {
        name: "config.llm",
        title: "LLM Providers",
        category: "system",
        keys: [],
        slashName: "llm",
        children: [
          { name: "config.llm.default", title: "Default Provider", category: "system", keys: [], slashName: "default" },
        ],
      },
      {
        name: "config.publish",
        title: "Publish Settings",
        category: "system",
        keys: [],
        slashName: "publish",
        children: [
          {
            name: "config.publish.templates",
            title: "Templates",
            category: "system",
            keys: [],
            slashName: "templates",
            children: [
              { name: "config.publish.templates.list", title: "List templates", category: "system", keys: [], slashName: "list" },
              { name: "config.publish.templates.add", title: "Add template", category: "system", keys: [], slashName: "add" },
              { name: "config.publish.templates.edit", title: "Edit template", category: "system", keys: [], slashName: "edit" },
              { name: "config.publish.templates.delete", title: "Delete template", category: "system", keys: [], slashName: "delete" },
            ],
          },
          {
            name: "config.publish.push",
            title: "Push Config",
            category: "system",
            keys: [],
            slashName: "push",
            children: [
              { name: "config.publish.push.privacy", title: "Privacy Clean ON/OFF", category: "system", keys: [], slashName: "privacy" },
              { name: "config.publish.push.format", title: "Commit Format", category: "system", keys: [], slashName: "format" },
              { name: "config.publish.push.release", title: "Release URL", category: "system", keys: [], slashName: "release_url" },
            ],
          },
        ],
      },
      {
        name: "config.bin",
        title: "Bin",
        category: "system",
        keys: [],
        slashName: "bin",
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
        name: "runtime.history",
        title: "Operation History",
        category: "action",
        keys: [],
        slashName: "history",
        children: [
          { name: "runtime.history.full", title: "Full history", category: "action", keys: [], slashName: "full" },
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
        name: "runtime.trial",
        title: "Trial (External PRs)",
        category: "action",
        keys: [],
        slashName: "trial",
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
  if (!root) return { node: null, children: visible(registry) };

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
  return visible(REGISTRY).filter((c) => !c.scene || c.scene.includes(scene));
}

/** Get suggestions for the current command input and scene. */
export function getCommands(scene: Scene): Suggestion[];
export function getCommands(scene: Scene, cmdValue: string): Suggestion[];
export function getCommands(scene: Scene, cmdValue?: string): Suggestion[] {
  const top = topLevelCommands(scene);

  if (!cmdValue || cmdValue.trim().length === 0) {
    return sortByName(top.map((c) => ({ label: "/" + c.slashName, description: c.title })));
  }

  // Strip leading slash if present, then parse path
  const input = cmdValue.startsWith("/") ? cmdValue.slice(1) : cmdValue;
  const tokens = input.trim().split(/\s+/);

  if (tokens.length === 0) {
    return sortByName(top.map((c) => ({ label: "/" + c.slashName, description: c.title })));
  }

  const firstToken = tokens[0].toLowerCase();

  // If only the first token is partially typed, filter top-level
  if (tokens.length === 1 && !input.endsWith(" ")) {
    return sortByName(
      top
        .filter((c) => c.slashName.startsWith(firstToken) || c.slashAliases?.some((a) => a.startsWith(firstToken)))
        .map((c) => ({ label: "/" + c.slashName, description: c.title })),
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
        .map((c) => ({ label: c.slashName, description: c.title })),
    );
  }

  // Full path resolved — show all children
  return sortByName(children.map((c) => ({ label: c.slashName, description: c.title })));
}

/** Returns all keybindings visible in the help panel for a given scene */
export function getKeybindings(scene: Scene): CommandDef[] {
  const defs = visible(REGISTRY).filter((c) => !c.scene || c.scene.includes(scene));
  return sortByName(defs);
}
