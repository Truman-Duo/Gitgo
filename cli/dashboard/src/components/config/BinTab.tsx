// src/components/config/BinTab.tsx — /config Bin tab (self-contained).
// Owns: delete-delay setting + archived-project restore/soft/hard delete.
// COMMAND input (/restore /soft-delete /hard-delete /cancel-delete) in archived view.

import React, { memo, useState, useEffect, useCallback, useMemo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import {
  configGet, configSet, listArchivedProjects,
  archiveProject, deleteProject, cancelPendingDelete,
} from "../../mcp/tools.js";
import {
  isCommandMode, resolveCommandKeys, applyCommandAction,
  type CommandAction, type CommandHandlers,
} from "../../input/commandInput.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { colors, useSelectionStyle } from "../../theme/index.js";
import type { ConfigTabProps } from "./types.js";

type BinView = "menu" | "archived" | "delay";
type ArchivedProject = { name: string; workspace: string; release_url: string; pending_hard_delete_at: string };
type BinConfirm = { type: "restore" | "soft_delete" | "hard_delete"; name: string } | null;

const DELETE_DELAY_OPTIONS = [
  { label: "Immediate", value: 0 },
  { label: "1 hour", value: 60 },
  { label: "1 day", value: 1440 },
  { label: "3 days", value: 4320 },
];

const BIN_MENU: { id: Exclude<BinView, "menu">; label: string }[] = [
  { id: "archived", label: "Archived Projects" },
  { id: "delay", label: "Delete Delay" },
];

type BinCtx = {
  binView: BinView;
  binConfirm: boolean;
  cmdValue: string;
  suggestionCount: number;
};

type BinAction =
  | { type: "esc" }
  | { type: "command"; action: CommandAction }
  | { type: "tabPrev" }
  | { type: "tabNext" }
  | { type: "binMenuMove"; delta: number }
  | { type: "binMenuConfirm" }
  | { type: "binDelayAdjust"; delta: number }
  | { type: "binArchMove"; delta: number }
  | { type: "binConfirmCancel" }
  | { type: "binConfirmMove"; index: number }
  | { type: "binConfirmYes" };

function resolveBinKey(ctx: BinCtx, input: string, key: any): BinAction[] {
  // Bin confirm dialog
  if (ctx.binConfirm) {
    if (matchChord("escape", input, key)) return [{ type: "binConfirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "binConfirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "binConfirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "binConfirmYes" }];
    return [];
  }

  // Esc (list mode)
  if (matchChord("escape", input, key)) return [{ type: "esc" }];

  // COMMAND mode
  if (isCommandMode(ctx.cmdValue, input)) {
    return resolveCommandKeys(ctx.cmdValue, ctx.suggestionCount, input, key).map((a) => ({ type: "command", action: a }));
  }

  // Tab switching via left/right
  if (matchChord("left", input, key)) return [{ type: "tabPrev" }];
  if (matchChord("right", input, key)) return [{ type: "tabNext" }];

  // Per-view content
  if (ctx.binView === "menu") {
    if (matchChord("up", input, key)) return [{ type: "binMenuMove", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "binMenuMove", delta: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "binMenuConfirm" }];
    return [];
  }
  if (ctx.binView === "delay") {
    if (matchChord("up", input, key)) return [{ type: "binDelayAdjust", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "binDelayAdjust", delta: 1 }];
    return [];
  }
  // archived
  if (matchChord("up", input, key)) return [{ type: "binArchMove", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "binArchMove", delta: 1 }];
  return [];
}

export const BinTab = memo(function BinTab({
  client, cmdInput, onFooter, onRefresh, report, shell,
}: ConfigTabProps) {
  const [deleteDelayIdx, setDeleteDelayIdx] = useState(2);
  const [binView, setBinView] = useState<BinView>("menu");
  const [binMenuIdx, setBinMenuIdx] = useState(0);
  const [archived, setArchived] = useState<ArchivedProject[]>([]);
  const [archivedIdx, setArchivedIdx] = useState(0);
  const [archivedLoading, setArchivedLoading] = useState(true);
  const [binConfirm, setBinConfirm] = useState<BinConfirm>(null);
  const [binConfirmSel, setBinConfirmSel] = useState(1);
  const [statusMsg, setStatusMsg] = useState("");
  const [suggestionIdx, setSuggestionIdx] = useState(0);

  const suggestionLabels = useMemo(() => {
    if (!cmdInput.value.startsWith("/")) return [];
    const prefix = cmdInput.value.slice(1).toLowerCase();
    const cmds = ["/restore", "/soft-delete", "/hard-delete", "/cancel-delete"];
    return cmds.filter((c) => c.slice(1).startsWith(prefix));
  }, [cmdInput.value]);

  // Load delete-delay setting from config.
  useEffect(() => {
    configGet(client).then((r: any) => {
      if (r?.safety?.delete_delay_minutes !== undefined) {
        const val = r.safety.delete_delay_minutes;
        const idx = DELETE_DELAY_OPTIONS.findIndex((o) => o.value === val);
        if (idx >= 0) setDeleteDelayIdx(idx);
      }
    }).catch(() => {});
  }, [client]);

  const refreshArchived = useCallback(async () => {
    const r: any = await listArchivedProjects(client);
    setArchived(r || []);
  }, [client]);

  useEffect(() => {
    listArchivedProjects(client).then((r: any) => {
      setArchived(r || []);
      setArchivedLoading(false);
    }).catch(() => setArchivedLoading(false));
  }, [client]);

  // Report sub (confirm dialog) to shell.
  useEffect(() => {
    report({ sub: binConfirm !== null, fullscreen: false });
  }, [report, binConfirm]);

  // Footer: COMMAND bar only in archived view.
  useEffect(() => {
    const barOn = binView === "archived";
    if (!barOn) {
      onFooter({ hidden: true });
      return () => onFooter(null);
    }
    onFooter({
      kind: "command",
      cmdInput,
      statusText: statusMsg || `Type ${chordLabel("slash")} for commands`,
      suggestions: suggestionLabels,
      suggestionIdx,
      cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput.value, cmdInput.cursor, statusMsg, suggestionLabels, suggestionIdx, onFooter, binView, cmdInput]);

  const runCommand = useCallback(async (cmd: string) => {
    const clean = cmd.replace(/^[:\/]\s*/, "").trim();
    setStatusMsg("");
    const p = archived[archivedIdx];
    if (!p) {
      setStatusMsg("No archived project selected");
    } else if (clean === "restore") {
      setBinConfirm({ type: "restore", name: p.name });
      setBinConfirmSel(0);
    } else if (clean === "soft-delete") {
      setBinConfirm({ type: "soft_delete", name: p.name });
      setBinConfirmSel(0);
    } else if (clean === "hard-delete") {
      setBinConfirm({ type: "hard_delete", name: p.name });
      setBinConfirmSel(0);
    } else if (clean === "cancel-delete") {
      cancelPendingDelete(client, p.name).then(() => { refreshArchived(); onRefresh?.(); });
    } else {
      setStatusMsg(`Unknown: ${clean}`);
    }
    cmdInput.setValue("");
  }, [archived, archivedIdx, client, refreshArchived, onRefresh, cmdInput]);

  const commandHandlers: CommandHandlers = {
    cmdInput,
    suggestionLabels,
    suggestionIdx,
    setSuggestionIdx,
    runCommand,
  };

  // ── Keyboard ────────────────────────────────────────────
  useInput((input: string, key: any) => {
    const ctx: BinCtx = {
      binView,
      binConfirm: binConfirm !== null,
      cmdValue: cmdInput.value,
      suggestionCount: suggestionLabels.length,
    };
    for (const a of resolveBinKey(ctx, input, key)) {
      switch (a.type) {
        case "esc":
          if (cmdInput.value.length > 0) {
            cmdInput.setValue("");
          } else if (binView !== "menu") {
            setBinView("menu");
          } else {
            shell.goToTab("providers");
          }
          break;
        case "command":
          applyCommandAction(a.action, commandHandlers);
          break;
        case "tabPrev":
          shell.tabPrev();
          break;
        case "tabNext":
          shell.tabNext();
          break;
        case "binMenuMove":
          setBinMenuIdx((s) => (s + a.delta + BIN_MENU.length) % BIN_MENU.length);
          break;
        case "binMenuConfirm": {
          const item = BIN_MENU[binMenuIdx];
          if (item) setBinView(item.id);
          break;
        }
        case "binDelayAdjust":
          setDeleteDelayIdx((s) => {
            const next = Math.max(0, Math.min(DELETE_DELAY_OPTIONS.length - 1, s + a.delta));
            const val = DELETE_DELAY_OPTIONS[next]?.value;
            if (val !== undefined) configSet(client, "safety.delete_delay_minutes", val).catch(() => {});
            return next;
          });
          break;
        case "binArchMove":
          setArchivedIdx((s) => Math.max(0, Math.min(archived.length - 1, s + a.delta)));
          break;
        case "binConfirmCancel":
          setBinConfirm(null);
          setBinConfirmSel(1);
          break;
        case "binConfirmMove":
          setBinConfirmSel(a.index);
          break;
        case "binConfirmYes": {
          if (binConfirmSel === 0 && binConfirm) {
            const { type, name } = binConfirm;
            if (type === "restore") archiveProject(client, name).then(() => { refreshArchived(); onRefresh?.(); });
            else if (type === "soft_delete") deleteProject(client, name, "soft").then(() => { refreshArchived(); onRefresh?.(); });
            else if (type === "hard_delete") deleteProject(client, name, "hard").then(() => { refreshArchived(); onRefresh?.(); });
          }
          setBinConfirm(null);
          setBinConfirmSel(1);
          break;
        }
      }
    }
  });

  // ── Render ──────────────────────────────────────────────
  if (binConfirm) {
    const labels: Record<string, string> = {
      restore: "restore",
      soft_delete: "soft delete (remove governance, keep files)",
      hard_delete: "hard delete (remove EVERYTHING)",
    };
    return (
      <Box flexDirection="column">
        <Box marginBottom={1}>
          <Text bold>
            Confirm {labels[binConfirm.type]}: {binConfirm.name}
          </Text>
        </Box>
        {binConfirm.type === "hard_delete" && (
          <Box marginBottom={1}>
            <Text color={colors.danger}>
              This will delete ALL project files.{"\n"}
              Deletion is delayed per Bin settings.
            </Text>
          </Box>
        )}
        <Box flexDirection="row" marginBottom={1}>
          {(() => {
            const yesStyle = useSelectionStyle(binConfirmSel === 0 ? "focused" : "non-focused", "block", "confirm-yes");
            const noStyle = useSelectionStyle(binConfirmSel === 1 ? "focused" : "non-focused", "block", "confirm-no");
            return (
              <>
                <Text color={yesStyle.fg} backgroundColor={yesStyle.bg} bold={yesStyle.bold}>Yes</Text>
                <Text>     </Text>
                <Text color={noStyle.fg} backgroundColor={noStyle.bg} bold={noStyle.bold}>No</Text>
              </>
            );
          })()}
          <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} confirm    {chordLabel("escape")} cancel</Text>
        </Box>
      </Box>
    );
  }

  if (binView === "menu") {
    return (
      <Box flexDirection="column" flexGrow={1}>
        {BIN_MENU.map((item, i) => {
          const active = i === binMenuIdx;
          const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
          return (
            <Text key={item.id} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
              {item.label}
            </Text>
          );
        })}
        <Box marginTop={1}>
          <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} open    {chordLabel("escape")} back</Text>
        </Box>
      </Box>
    );
  }

  if (binView === "delay") {
    return (
      <Box flexDirection="column" flexGrow={1}>
        <Box flexDirection="column" marginBottom={1}>
          <Text dimColor bold>▸ Delete Delay</Text>
          {DELETE_DELAY_OPTIONS.map((opt, i) => {
            const active = i === deleteDelayIdx;
            const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
            return (
              <Text key={opt.value} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                {opt.label}
              </Text>
            );
          })}
        </Box>
        <Box marginTop={1}>
          <Text dimColor>{chordLabel("upDown")} adjust delay    {chordLabel("escape")} back</Text>
        </Box>
      </Box>
    );
  }

  // binView === "archived"
  return (
    <Box flexDirection="column" flexGrow={1}>
      <Box flexDirection="column">
        <Text dimColor bold>▸ Archived Projects</Text>
        {archivedLoading ? (
          <Text dimColor>Loading...</Text>
        ) : archived.length === 0 ? (
          <Text dimColor>No archived projects.</Text>
        ) : (
          archived.map((p, i) => {
            const active = i === archivedIdx;
            const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
            const pending = p.pending_hard_delete_at
              ? `  pending delete (${p.pending_hard_delete_at.slice(0, 16)})`
              : "";
            return (
              <Box key={p.name} flexDirection="column">
                <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                  {p.name}{pending}
                </Text>
                {active && p.workspace ? (
                  <Box paddingLeft={2}><Text dimColor>workspace: {p.workspace}</Text></Box>
                ) : null}
              </Box>
            );
          })
        )}
      </Box>
      <Box marginTop={1}>
        <Text dimColor>
          {chordLabel("upDown")} select    /restore /soft-delete /hard-delete /cancel-delete    {chordLabel("escape")} back
        </Text>
      </Box>
    </Box>
  );
});
