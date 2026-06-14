// src/components/App.tsx
import React, { useState, useCallback, useMemo } from "react";
import { Box, Text, useInput, useApp, useTerminalSize } from "@anthropic/ink";
import { McpClient } from "../mcp/client.js";
import { useGitgoData } from "../hooks/useGitgoData.js";
import { Overview } from "./Overview.js";
import { Detail } from "./Detail.js";
import { CommandBar, type Suggestion } from "./CommandBar.js";
import { HelpPanel } from "./HelpPanel.js";

type Props = { client: McpClient; refreshSec?: number };
type FocusTarget = "table" | "command";

const COMMANDS: Suggestion[] = [
  { label: "lesson",   description: "list pending lessons" },
  { label: "contract", description: "show contract summary" },
  { label: "status",   description: "show project status" },
  { label: "verify",   description: "verify a lesson by ID" },
  { label: "project",  description: "jump to a project" },
  { label: "refresh",  description: "force refresh data" },
  { label: "help",     description: "show help panel" },
];

export function App({ client, refreshSec = 5 }: Props) {
  const { exit } = useApp();
  const { columns: termCols, rows: termRows } = useTerminalSize();
  const { projects, loading, error, refresh } = useGitgoData(
    client,
    refreshSec
  );
  const [sel, setSel] = useState(0);
  const [detail, setDetail] = useState(false);
  const [focus, setFocus] = useState<FocusTarget>("table");
  const [cmdBuf, setCmdBuf] = useState("");
  const [cmdCursor, setCmdCursor] = useState(0);
  const [cmdResult, setCmdResult] = useState("");
  const [showHelp, setShowHelp] = useState(false);
  const [cmdHistory, setCmdHistory] = useState<string[]>([]);
  const [cmdHistoryIdx, setCmdHistoryIdx] = useState(-1);
  const [suggestionIdx, setSuggestionIdx] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);

  // Show matching commands based on text after ":"
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

  const handleDismissHelp = useCallback(() => setShowHelp(false), []);
  const handleBackFromDetail = useCallback(() => setDetail(false), []);

  const enterCommandMode = useCallback(() => {
    setFocus("command");
    setCmdBuf("");
    setCmdCursor(0);
    setCmdResult("");
    setSuggestionIdx(0);
  }, []);

  const exitCommandMode = useCallback(() => {
    setFocus("table");
    setCmdBuf("");
    setCmdCursor(0);
    setCmdHistoryIdx(-1);
    setSuggestionIdx(0);
  }, []);

  const executeCommand = useCallback(
    async (cmd: string) => {
      setCmdHistory((prev) => [...prev, cmd]);
      setCmdHistoryIdx(-1);
      // Strip leading ":" and whitespace
      const clean = cmd.replace(/^:\s*/, "");
      const parts = clean.split(/\s+/);
      const action = parts[0]?.toLowerCase();
      const target = parts[1];
      try {
        switch (action) {
          case "l":
          case "lesson": {
            const name = target || projects[sel]?.name;
            if (!name) { setCmdResult("No project selected"); return; }
            const result: any = await client.callTool("gitgo_lesson_list", { project: name });
            const pending = result?.pending || [];
            if (!pending.length) { setCmdResult(`${name}: no pending lessons`); return; }
            const lines = pending.slice(0, 5).map((l: any) =>
              `[${(l.severity||"?")[0]?.toUpperCase()}] ${l.id?.slice(0,8)||"?"} ${l.trigger?.slice(0,40)}`
            );
            setCmdResult(`${name}: ${pending.length} pending  |  ${lines.join("  |  ")}${pending.length>5?" ...":""}`);
            break;
          }
          case "c":
          case "contract": {
            const name = target || projects[sel]?.name;
            if (!name) { setCmdResult("No project selected"); return; }
            const contract: any = await client.callTool("gitgo_contract_show", { project: name });
            if (!contract || contract.error) { setCmdResult(`${name}: no contract`); return; }
            const f = contract.decided_features?.length || 0;
            const c = contract.architecture_constraints?.length || 0;
            const ts = contract.tech_stack?.join(",") || "?";
            setCmdResult(`${name}: ${f}f/${c}c  tech:${ts}  updated:${contract.updated_at?.slice(0,10)||"?"}`);
            break;
          }
          case "s":
          case "status": {
            const name = target || projects[sel]?.name;
            if (!name) { setCmdResult("No project selected"); return; }
            const status: any = await client.callTool("gitgo_status", { project: name });
            const ws = status?.workspace || {};
            const commits = status?.commits || {};
            setCmdResult(
              `${name}  stage:${status?.stage||"?"}  ` +
              `changed:${ws.entries_changed||0}/${ws.entries_total||0}  ` +
              `formal:${commits.formal_synced||0}/${commits.formal_total||0}  ` +
              `next:${status?.semantic?.suggested_next_action||"?"}`
            );
            break;
          }
          case "v":
          case "verify": {
            if (!target) { setCmdResult(":v <lesson_id> — verify a lesson"); return; }
            const name = projects[sel]?.name;
            if (!name) { setCmdResult("No project selected"); return; }
            const result: any = await client.callTool("gitgo_lesson_verify", {
              project: name, lesson_id: target,
            });
            if (result?.error) { setCmdResult(`Verify failed: ${result.error}`); }
            else { setCmdResult(`Verified ${target.slice(0,12)} (count:${result?.verified_count||0})`); }
            break;
          }
          case "p":
          case "project": {
            if (!target) { setCmdResult(":p <name> — jump to project"); return; }
            const idx = projects.findIndex((p) =>
              p.name.toLowerCase() === target.toLowerCase()
            );
            if (idx >= 0) { setSel(idx); setCmdResult(`Jumped to ${projects[idx].name}`); }
            else { setCmdResult(`Project not found: ${target}`); }
            break;
          }
          case "r":
          case "refresh":
            await refresh();
            setRefreshKey((k) => k + 1);
            setCmdResult("Refreshed");
            break;
          case "h":
          case "help":
            setShowHelp(true);
            setCmdResult("");
            break;
          default:
            setCmdResult(`Unknown: ${cmd}  (:h for help)`);
        }
      } catch (e: any) {
        setCmdResult(`Error: ${e.message}`);
      }
      setFocus("table");
    },
    [projects, sel, client, refresh]
  );

  useInput((input: string, key: any) => {
    // ── Command mode ──────────────────────────────────────
    if (focus === "command") {
      if (key.return) {
        const cmd = cmdBuf.trim();
        if (cmd) executeCommand(cmd);
        else exitCommandMode();
        return;
      }
      if (key.escape) { exitCommandMode(); return; }

      // Cursor movement
      if (key.leftArrow)  { setCmdCursor((c) => Math.max(0, c - 1)); return; }
      if (key.rightArrow) { setCmdCursor((c) => Math.min(cmdBuf.length, c + 1)); return; }
      if (key.home)       { setCmdCursor(0); return; }
      if (key.end)        { setCmdCursor(cmdBuf.length); return; }

      // Deletion
      if (key.backspace) {
        if (cmdCursor > 0) {
          setCmdBuf((prev) => prev.slice(0, cmdCursor - 1) + prev.slice(cmdCursor));
          setCmdCursor((c) => c - 1);
        }
        return;
      }
      if (key.delete) {
        if (cmdCursor < cmdBuf.length) {
          setCmdBuf((prev) => prev.slice(0, cmdCursor) + prev.slice(cmdCursor + 1));
        }
        return;
      }

      // Up arrow in empty buffer → exit; with suggestions → cycle selection
      if (key.upArrow) {
        if (!cmdBuf.trim()) { exitCommandMode(); return; }
        if (suggestions.length > 0) {
          setSuggestionIdx((p) => (p - 1 + suggestions.length) % suggestions.length);
          return;
        }
        exitCommandMode();
        return;
      }
      if (key.downArrow && suggestions.length > 0) {
        setSuggestionIdx((p) => (p + 1) % suggestions.length);
        return;
      }

      // Tab — cycle through suggestions and fill (keeps ":" prefix)
      if (key.tab && suggestions.length > 0) {
        const s = suggestions[suggestionIdx % suggestions.length];
        if (s) {
          setCmdBuf(":" + s.label + " ");
          setCmdCursor(s.label.length + 2);
        }
        setSuggestionIdx((prev) => (prev + 1) % suggestions.length);
        return;
      }

      // Character insertion (handles paste — multi-char input)
      if (input && input.length >= 1 && !key.ctrl && !key.meta) {
        setCmdBuf((prev) => prev.slice(0, cmdCursor) + input + prev.slice(cmdCursor));
        setCmdCursor((c) => c + input.length);
        return;
      }
      return;
    }

    // ── Help mode ─────────────────────────────────────────
    if (showHelp) {
      if (key.escape || input === "h" || input === "q") { setShowHelp(false); }
      return;
    }

    // ── Detail mode ───────────────────────────────────────
    if (detail) {
      if (input === "q") { setDetail(false); return; }
      // Esc handled by Detail component (L3→L2→exit)
      return;
    }

    // ── Overview (table focus) ────────────────────────────
    if (input === "q") { exit(); return; }
    if (input === ":") { enterCommandMode(); return; }
    if (input === "h") { setShowHelp((prev) => !prev); return; }
    if (key.return && projects.length > 0) { setDetail(true); return; }
    if (key.upArrow) {
      if (sel === 0) { enterCommandMode(); }
      else { setSel((prev) => Math.max(0, prev - 1)); }
      return;
    }
    if (key.downArrow) {
      if (sel >= projects.length - 1) { enterCommandMode(); }
      else { setSel((prev) => Math.min(projects.length - 1, prev + 1)); }
      return;
    }
  });

  if (loading) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text dimColor>Loading projects...</Text>
      </Box>
    );
  }

  const responsiveWidth = Math.max(60, termCols || 80);

  return (
    <Box flexDirection="column" width={responsiveWidth}>
      {error ? (
        <Box paddingLeft={1}>
          <Text color="red">Error: {error}</Text>
        </Box>
      ) : null}

      {showHelp ? (
        <HelpPanel onDismiss={handleDismissHelp} />
      ) : detail && projects[sel] ? (
        <Detail
          projectName={projects[sel].name}
          client={client}
          onBack={handleBackFromDetail}
          cols={responsiveWidth}
          rows={termRows || 24}
          refreshKey={refreshKey}
        />
      ) : (
        <Overview projects={projects} sel={sel} focus={focus} cols={responsiveWidth} />
      )}

      {/* Spacer */}
      <Box flexGrow={1} />

      <Box marginTop={1}>
        <CommandBar
          buf={focus === "command" ? cmdBuf : null}
          cursor={cmdCursor}
          result={cmdResult}
          suggestions={suggestions}
          suggestionIdx={suggestionIdx}
          cols={responsiveWidth}
        />
      </Box>
    </Box>
  );
}
