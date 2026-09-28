import React, {memo, useCallback, useEffect, useMemo, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {archiveProject, configGet, configSet} from "../backend/tools.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {
  applyCommandAction, isCommandMode, resolveCommandKeys, type CommandHandlers,
} from "../input/commandInput.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {colors, separator, usePanelSize, useSelectionStyle} from "../theme/index.js";
import type {FooterConfig, Suggestion} from "./CommandBar.js";
import {clampHeaderIndex, HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";

type TabId = "targets" | "plans" | "delay";
type Target = {
  scope: "project" | "process";
  project: string;
  process_id?: string;
  display_name?: string;
  status?: string;
};
type Confirmation = {planId: string; message: string};

const TABS = [
  {id: "targets", label: "Projects and Subprocesses"},
  {id: "plans", label: "Deletion Plans"},
  {id: "delay", label: "Delete Delay"},
] as const;
const DELAYS = [0, 10, 60, 1440, 4320];

type Props = {
  client: BackendClient;
  cmdInput: UseTextInputReturn;
  onFooter: (config: FooterConfig | null) => void;
  onDismiss: () => void;
  onRefresh?: () => void;
};

export const BinWorkspace = memo(function BinWorkspace({
  client, cmdInput, onFooter, onDismiss, onRefresh,
}: Props) {
  const {w} = usePanelSize({minWidth: 40, widthOffset: 4});
  const [tab, setTab] = useState<TabId>("targets");
  const [index, setIndex] = useState(0);
  const [suggestion, setSuggestion] = useState(0);
  const [data, setData] = useState<any>({projects: [], processes: []});
  const [plans, setPlans] = useState<any[]>([]);
  const [status, setStatus] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [confirmNo, setConfirmNo] = useState(true);
  const [delays, setDelays] = useState({project: 10, process: 10});
  const [delayRow, setDelayRow] = useState(0);
  const [editing, setEditing] = useState(false);

  const refresh = useCallback(async () => {
    const [targets, items] = await Promise.all([
      client.callTool("bin.targets", {}),
      client.callTool("deletion.list"),
    ]);
    setData(targets || {});
    setPlans(Array.isArray(items) ? items : []);
  }, [client]);

  useEffect(() => {
    void refresh().catch(error => setStatus(String(error?.message || error)));
    void configGet(client).then((value: any) => setDelays({
      project: Number(value?.safety?.delete_delay_minutes ?? 10),
      process: Number(value?.safety?.process_delete_delay_minutes ?? 10),
    })).catch(error => setStatus(String(error?.message || error)));
  }, [client, refresh]);

  const targets = useMemo<Target[]>(() => [
    ...(data.projects || []).map((item: any) => ({
      scope: "project" as const, project: item.name, ...item,
    })),
    ...(data.processes || []).map((item: any) => ({
      scope: "process" as const, project: item.project, ...item,
    })),
  ], [data]);
  const rows: any[] = tab === "targets" ? targets : tab === "plans" ? plans : [];
  const commands = tab === "targets"
    ? ["/restore", "/soft-delete", "/hard-delete"]
    : tab === "plans" ? ["/cancel", "/retry"] : [];
  const suggestions = useMemo<Suggestion[]>(() => cmdInput.value.startsWith("/")
    ? commands.filter(command => command.slice(1).startsWith(cmdInput.value.slice(1).toLowerCase()))
      .map(label => ({label, description: "", inputMode: "execute"}))
    : [], [cmdInput.value, commands.join("|")]);

  useEffect(() => {
    setIndex(value => Math.max(0, Math.min(value, Math.max(0, rows.length - 1))));
  }, [rows.length]);
  useEffect(() => {
    if (tab === "delay") onFooter({hidden: true});
    else onFooter({
      kind: "command", cmdInput,
      statusText: status || `Type ${chordLabel("slash")} for commands`,
      suggestions, suggestionIdx: suggestion, cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput, cmdInput.value, cmdInput.cursor, onFooter, status, suggestion, suggestions, tab]);

  const moveTab = (delta: number) => {
    const current = TABS.findIndex(item => item.id === tab);
    setTab(TABS[clampHeaderIndex(current, delta, TABS.length)]!.id);
    setIndex(0);
  };

  const prepare = useCallback(async (mode: "soft" | "hard") => {
    const target = targets[index];
    if (!target) { setStatus("Nothing selected"); return; }
    const args: any = {project: target.project, scope: target.scope, mode};
    if (target.scope === "process") args.process_id = target.process_id;
    const plan: any = await client.callTool("deletion.preview", args);
    const manifest = plan.manifest;
    const label = target.scope === "project" ? target.project : target.display_name || "Subprocess";
    const cascade = target.scope === "project" && manifest.b_process_count
      ? `\nIncludes ${manifest.b_process_count} subprocess(es); ${manifest.unfinished_b_process_count || 0} unfinished.`
      : "";
    setConfirmation({
      planId: plan.plan_id,
      message: `${mode.toUpperCase()} delete ${label}${cascade}\n${manifest.effect}\nDelay: ${manifest.delay_minutes} min; worktrees: ${manifest.worktrees.length}`,
    });
    setConfirmNo(true);
  }, [client, index, targets]);

  const runCommand = useCallback(async (raw: string) => {
    const command = raw.replace(/^[:/]\s*/, "").trim();
    setStatus("");
    try {
      const selected = rows[index];
      if (command === "restore") {
        if (selected?.scope === "project") await archiveProject(client, selected.project);
        else if (selected?.scope === "process") await client.callTool("process.archive", {
          project: selected.project, process_id: selected.process_id, archived: false,
        });
        else throw new Error("Select an archived item");
      } else if (command === "soft-delete" || command === "hard-delete") {
        await prepare(command.startsWith("soft") ? "soft" : "hard");
      } else if (command === "cancel" && selected) {
        await client.callTool("deletion.cancel", {plan_id: selected.plan_id});
      } else if (command === "retry" && selected) {
        await client.callTool("deletion.retry", {plan_id: selected.plan_id});
      } else throw new Error("Unknown command: " + command);
      if (!command.endsWith("delete")) { await refresh(); onRefresh?.(); }
    } catch (error: any) { setStatus(String(error?.message || error)); }
    cmdInput.setValue("");
  }, [client, cmdInput, index, onRefresh, prepare, refresh, rows]);

  const handlers: CommandHandlers = {
    cmdInput, suggestionLabels: suggestions.map(item => item.label), suggestionIdx: suggestion,
    setSuggestionIdx: setSuggestion, runCommand,
  };

  useInput((input: string, key: any) => {
    if (confirmation) {
      if (matchChord("escape", input, key)) setConfirmation(null);
      else if (matchChord("left", input, key) || matchChord("right", input, key)) {
        setConfirmNo(value => !value);
      } else if (matchChord("enter", input, key)) {
        if (confirmNo) { setConfirmation(null); return; }
        const planId = confirmation.planId;
        setConfirmation(null);
        setStatus("Scheduling deletion…");
        void client.callTool("deletion.confirm", {plan_id: planId}).then(async (plan: any) => {
          const result = plan.not_before * 1000 <= Date.now()
            ? await client.callTool("deletion.run", {plan_id: planId}, 120) : plan;
          setStatus(result.state === "completed" ? "Deletion completed"
            : `Deletion ${result.state}${result.error ? ": " + result.error : ""}`);
          await refresh();
          onRefresh?.();
        }).catch(error => setStatus(String(error?.message || error)));
      }
      return;
    }
    if (isCommandMode(cmdInput.value, input)) {
      for (const action of resolveCommandKeys(
        cmdInput.value, suggestions.length, input, key,
        suggestions[suggestion % Math.max(1, suggestions.length)]?.label || "",
        suggestions[suggestion % Math.max(1, suggestions.length)]?.inputMode || "fill",
      )) {
        applyCommandAction(action, handlers);
      }
      return;
    }
    if (matchChord("escape", input, key)) {
      if (editing) setEditing(false); else onDismiss();
      return;
    }
    if (tab === "delay") {
      if (matchChord("up", input, key) && !editing) setDelayRow(value => Math.max(0, value - 1));
      else if (matchChord("down", input, key) && !editing) setDelayRow(value => Math.min(1, value + 1));
      else if (matchChord("enter", input, key)) {
        if (!editing) setEditing(true);
        else {
          const name = delayRow === 0 ? "delete_delay_minutes" : "process_delete_delay_minutes";
          const value = delayRow === 0 ? delays.project : delays.process;
          void configSet(client, "safety." + name, value)
            .then(() => setStatus("Delete delay saved"))
            .catch(error => setStatus(String(error?.message || error)));
          setEditing(false);
        }
      } else if (editing && (matchChord("left", input, key) || matchChord("right", input, key))) {
        const field = delayRow === 0 ? "project" : "process";
        const current = delays[field];
        const position = Math.max(0, DELAYS.findIndex(value => value >= current));
        const delta = matchChord("left", input, key) ? -1 : 1;
        setDelays(value => ({...value, [field]: DELAYS[Math.max(0, Math.min(DELAYS.length - 1, position + delta))]}));
      } else if (!editing && (matchChord("left", input, key) || matchChord("right", input, key))) {
        moveTab(matchChord("left", input, key) ? -1 : 1);
      }
      return;
    }
    if (matchChord("left", input, key) || matchChord("right", input, key)) {
      moveTab(matchChord("left", input, key) ? -1 : 1);
    } else if (matchChord("up", input, key)) setIndex(value => Math.max(0, value - 1));
    else if (matchChord("down", input, key)) setIndex(value => Math.min(Math.max(0, rows.length - 1), value + 1));
  });

  if (confirmation) return <Box flexDirection="column">
    <Text bold>Confirm deletion</Text>
    <Text>{confirmation.message}</Text>
    <Box marginTop={1}>
      <Text {...useSelectionStyle(!confirmNo ? "focused" : "non-focused", "block")}>Delete</Text>
      <Text>   </Text>
      <Text {...useSelectionStyle(confirmNo ? "focused" : "non-focused", "block")}>Cancel</Text>
    </Box>
    <Text dimColor>{chordLabel("leftRight")} select   {chordLabel("enter")} confirm   {chordLabel("escape")} cancel</Text>
  </Box>;

  const visibleStart = Math.max(0, index - 5);
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
    <HorizontalHeaderStrip items={[...TABS]} selected={TABS.findIndex(item => item.id === tab)} width={w}/>
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <Box flexDirection="column" flexGrow={1} paddingTop={1}>
      {tab === "delay" ? <>{(["Project", "Subprocess"] as const).map((label, rowIndex) =>
        <Text key={label} {...useSelectionStyle(rowIndex === delayRow ? "focused" : "non-focused", "row")}>
          {label}   {rowIndex === 0 ? delays.project : delays.process} min{editing && rowIndex === delayRow ? "  editing" : ""}
        </Text>)}</> : rows.length === 0 ? <Text dimColor>Nothing archived here.</Text>
        : rows.slice(visibleStart, visibleStart + 12).map((item: any, offset: number) => {
          const absolute = visibleStart + offset;
          const label = tab === "targets"
            ? item.scope === "project" ? `Project   ${item.project}` : `Subprocess   ${item.display_name || "Subprocess"} (${item.project})`
            : `${item.manifest?.project || "?"} / ${item.manifest?.scope || "?"}   ${item.state}${item.error ? "   " + item.error : ""}`;
          return <Text key={item.plan_id || item.process_id || item.project}
            {...useSelectionStyle(absolute === index ? "focused" : "non-focused", "row")}>
            {label}
          </Text>;
        })}
      <Box flexGrow={1}/>
      <Text dimColor>{status || (tab === "delay"
        ? editing ? `${chordLabel("leftRight")} change   ${chordLabel("enter")} save`
          : `${chordLabel("upDown")} select   ${chordLabel("enter")} edit`
        : `${chordLabel("leftRight")} tabs   ${chordLabel("upDown")} select   ${commands.join("   ")}`)}   {chordLabel("escape")} back</Text>
    </Box>
  </Box>;
});
