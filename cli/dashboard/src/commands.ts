// src/commands.ts — command execution handlers extracted from App.tsx
import type { McpClient } from "./mcp/client.js";
import type { ProjectRow } from "./hooks/useGitgoData.js";
import type { Suggestion } from "./components/CommandBar.js";

export const COMMANDS: Suggestion[] = [
  { label: "lesson",   description: "list pending lessons" },
  { label: "contract", description: "show contract summary" },
  { label: "status",   description: "show project status" },
  { label: "verify",   description: "verify a lesson by ID" },
  { label: "project",  description: "jump to a project" },
  { label: "refresh",  description: "force refresh data" },
  { label: "help",     description: "show help panel" },
  { label: "llm",      description: "open LLM provider config" },
];

export type CommandContext = {
  client: McpClient;
  projects: ProjectRow[];
  sel: number;
  refresh: () => Promise<void>;
};

export type CommandOutcome = {
  resultText: string;
  jumpToProject?: number;   // :p target index
  showHelp?: boolean;        // :h
  refreshTrigger?: boolean;  // :r
};

export async function executeCommand(
  cmd: string,
  ctx: CommandContext,
): Promise<CommandOutcome> {
  const clean = cmd.replace(/^:\s*/, "");
  const parts = clean.split(/\s+/);
  const action = parts[0]?.toLowerCase();
  const target = parts[1];

  try {
    switch (action) {
      case "l":
      case "lesson": {
        const name = target || ctx.projects[ctx.sel]?.name;
        if (!name) return { resultText: "No project selected" };
        const result: any = await ctx.client.callTool("gitgo_lesson_list", { project: name });
        const pending = result?.pending || [];
        if (!pending.length) return { resultText: `${name}: no pending lessons` };
        const lines = pending.slice(0, 5).map((l: any) =>
          `[${(l.severity||"?")[0]?.toUpperCase()}] ${l.id?.slice(0,8)||"?"} ${l.trigger?.slice(0,40)}`
        );
        return { resultText: `${name}: ${pending.length} pending  |  ${lines.join("  |  ")}${pending.length>5?" ...":""}` };
      }
      case "c":
      case "contract": {
        const name = target || ctx.projects[ctx.sel]?.name;
        if (!name) return { resultText: "No project selected" };
        const contract: any = await ctx.client.callTool("gitgo_contract_show", { project: name });
        if (!contract || contract.error) return { resultText: `${name}: no contract` };
        const f = contract.decided_features?.length || 0;
        const c = contract.architecture_constraints?.length || 0;
        const ts = contract.tech_stack?.join(",") || "?";
        return { resultText: `${name}: ${f}f/${c}c  tech:${ts}  updated:${contract.updated_at?.slice(0,10)||"?"}` };
      }
      case "s":
      case "status": {
        const name = target || ctx.projects[ctx.sel]?.name;
        if (!name) return { resultText: "No project selected" };
        const status: any = await ctx.client.callTool("gitgo_status", { project: name });
        const ws = status?.workspace || {};
        const commits = status?.commits || {};
        return { resultText:
          `${name}  stage:${status?.stage||"?"}  ` +
          `changed:${ws.entries_changed||0}/${ws.entries_total||0}  ` +
          `formal:${commits.formal_synced||0}/${commits.formal_total||0}  ` +
          `next:${status?.semantic?.suggested_next_action||"?"}`
        };
      }
      case "v":
      case "verify": {
        if (!target) return { resultText: ":v <lesson_id> — verify a lesson" };
        const name = ctx.projects[ctx.sel]?.name;
        if (!name) return { resultText: "No project selected" };
        const result: any = await ctx.client.callTool("gitgo_lesson_verify", {
          project: name, lesson_id: target,
        });
        if (result?.error) return { resultText: `Verify failed: ${result.error}` };
        return { resultText: `Verified ${target.slice(0,12)} (count:${result?.verified_count||0})` };
      }
      case "p":
      case "project": {
        if (!target) return { resultText: ":p <name> — jump to project" };
        const idx = ctx.projects.findIndex((p) =>
          p.name.toLowerCase() === target.toLowerCase()
        );
        if (idx >= 0) return { resultText: `Jumped to ${ctx.projects[idx].name}`, jumpToProject: idx };
        return { resultText: `Project not found: ${target}` };
      }
      case "r":
      case "refresh":
        await ctx.refresh();
        return { resultText: "Refreshed", refreshTrigger: true };
      case "h":
      case "help":
        return { resultText: "", showHelp: true };
      default:
        return { resultText: `Unknown: ${cmd}  (:h for help)` };
    }
  } catch (e: any) {
    return { resultText: `Error: ${e.message}` };
  }
}
