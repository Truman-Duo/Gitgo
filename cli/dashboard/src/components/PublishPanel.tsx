import React, {memo, useCallback, useEffect, useMemo, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {publishGet, publishSet} from "../backend/tools.js";
import {applyTextOp} from "../hooks/useTextInput.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {colors, separator, usePanelSize} from "../theme/index.js";
import type {FooterConfig} from "./CommandBar.js";
import {clampHeaderIndex, HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";

const TABS = [
  {id: "commit", label: "Commit Format"},
  {id: "privacy", label: "Privacy"},
  {id: "remote", label: "Remote"},
] as const;

const ROWS = {
  commit: ["prefix", "number_start", "padding", "plugins", "template_name"],
  privacy: [
    "enabled", "deep_scan", "content_level", "severity_threshold",
    "exclude_paths", "include_paths", "ignored_rules", "approved_fingerprints",
  ],
  remote: ["remote_url", "trial", "formal"],
} as const;

const LABELS: Record<string, string> = {
  prefix: "Prefix", number_start: "Number Start", padding: "Padding",
  plugins: "Commit Plugins", template_name: "Template", enabled: "Privacy Scan",
  deep_scan: "Deep Scan", content_level: "Content Level",
  severity_threshold: "Minimum Severity", exclude_paths: "Excluded Paths",
  include_paths: "Explicit Includes", ignored_rules: "Ignored Rules",
  approved_fingerprints: "Approved Matches", remote_url: "Release URL",
  trial: "Trial Changes", formal: "Formal Commits",
};

const PUBLISH_ACTIVITY = new Set([
  "scan", "trial", "formalize", "sync", "push",
  "governance_synced", "governance_pushed", "governance_dissolved",
]);
const ARRAY_FIELDS = new Set([
  "plugins", "exclude_paths", "include_paths", "ignored_rules", "approved_fingerprints",
]);

type TabId = keyof typeof ROWS;
type PublishData = {
  commit_format: Record<string, unknown>;
  privacy: Record<string, unknown>;
  remote_url: string;
  stages?: {trial_configured?: boolean; release_configured?: boolean};
};
type Props = {
  client: BackendClient; project: string; cmdInput: UseTextInputReturn;
  onFooter: (config: FooterConfig | null) => void; onDismiss: () => void;
  onOpenTrial: () => void; onOpenFormal: () => void;
};

function parseEditedValue(field: string, raw: string): unknown {
  if (ARRAY_FIELDS.has(field)) return raw.split(";").map(value => value.trim()).filter(Boolean);
  if (field === "number_start") {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) throw new Error("Number Start must be a non-negative integer");
    return value;
  }
  if (field === "content_level") {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 1 || value > 3) throw new Error("Content Level must be 1, 2 or 3");
    return value;
  }
  return raw;
}

export const PublishPanel = memo(function PublishPanel({
  client, project, cmdInput, onFooter, onDismiss, onOpenTrial, onOpenFormal,
}: Props) {
  const {w} = usePanelSize({minWidth: 40, widthOffset: 4});
  const [tab, setTab] = useState<TabId>("commit");
  const [row, setRow] = useState(0);
  const [data, setData] = useState<PublishData | null>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const rows = useMemo<readonly string[]>(() => ROWS[tab], [tab]);

  const refresh = useCallback(async () => {
    const [value, events] = await Promise.all([
      publishGet(client, project),
      client.callTool("history.list", {project, limit: 100}),
    ]);
    setData(value as PublishData);
    setHistory((Array.isArray(events) ? events : [])
      .filter(item => PUBLISH_ACTIVITY.has(String(item.operation))).slice(-8));
  }, [client, project]);

  useEffect(() => { void refresh().catch(error => setStatus(String(error?.message || error))); }, [refresh]);
  useEffect(() => { setRow(value => Math.min(value, rows.length - 1)); }, [rows]);
  useEffect(() => {
    onFooter(editing ? {
      kind: "normal", cmdInput,
      statusText: `${chordLabel("enter")} save   ${chordLabel("escape")} cancel`,
      suggestions: [], suggestionIdx: 0, cmdResult: "",
    } : {hidden: true});
    return () => onFooter(null);
  }, [editing, cmdInput, cmdInput.value, cmdInput.cursor, onFooter]);

  const valueFor = useCallback((field: string): unknown => {
    if (!data) return undefined;
    if (field === "remote_url") return data.remote_url;
    return tab === "commit" ? data.commit_format[field] : data.privacy[field];
  }, [data, tab]);

  const beginEdit = (field: string) => {
    const value = valueFor(field);
    setEditing(field);
    cmdInput.setValue(Array.isArray(value) ? value.join("; ") : String(value ?? ""));
  };

  const save = useCallback(async () => {
    if (!editing || !data) return;
    try {
      const value = parseEditedValue(editing, cmdInput.value);
      if (tab === "commit") {
        await publishSet(client, project, "commit_format", {...data.commit_format, [editing]: value});
      } else if (tab === "privacy") {
        await publishSet(client, project, "privacy", {...data.privacy, [editing]: value});
      } else await publishSet(client, project, "remote_url", value);
      setEditing(null);
      cmdInput.setValue("");
      setStatus("Saved");
      await refresh();
    } catch (error: any) { setStatus(String(error?.message || error)); }
  }, [client, cmdInput, data, editing, project, refresh, tab]);

  const toggle = useCallback(async (field: string) => {
    if (!data) return;
    const section = tab === "commit" ? "commit_format" : "privacy";
    const owner = tab === "commit" ? data.commit_format : data.privacy;
    await publishSet(client, project, section, {...owner, [field]: !owner[field]});
    setStatus("Saved");
    await refresh();
  }, [client, data, project, refresh, tab]);

  useInput((input: string, key: any) => {
    if (editing) {
      if (matchChord("escape", input, key)) { setEditing(null); cmdInput.setValue(""); }
      else if (matchChord("enter", input, key)) void save();
      else if (matchChord("backspace", input, key)) applyTextOp({op: "delete_back"}, cmdInput);
      else if (matchChord("delete", input, key)) applyTextOp({op: "delete_forward"}, cmdInput);
      else if (matchChord("left", input, key)) applyTextOp({op: "move_cursor", delta: -1}, cmdInput);
      else if (matchChord("right", input, key)) applyTextOp({op: "move_cursor", delta: 1}, cmdInput);
      else if (matchChord("home", input, key)) applyTextOp({op: "move_to_start"}, cmdInput);
      else if (matchChord("end", input, key)) applyTextOp({op: "move_to_end"}, cmdInput);
      else if (input && !key.ctrl && !key.meta) applyTextOp({op: "insert", text: input}, cmdInput);
      return;
    }
    if (matchChord("escape", input, key)) { onDismiss(); return; }
    if (matchChord("left", input, key) || matchChord("right", input, key)) {
      const current = TABS.findIndex(item => item.id === tab);
      const delta = matchChord("left", input, key) ? -1 : 1;
      setTab(TABS[clampHeaderIndex(current, delta, TABS.length)]!.id);
      setRow(0);
      return;
    }
    if (matchChord("up", input, key)) { setRow(value => Math.max(0, value - 1)); return; }
    if (matchChord("down", input, key)) { setRow(value => Math.min(rows.length - 1, value + 1)); return; }
    if (!matchChord("enter", input, key) || !data) return;
    const field = rows[row]!;
    if (field === "trial") onOpenTrial();
    else if (field === "formal") onOpenFormal();
    else if (["enabled", "deep_scan", "padding"].includes(field)) {
      void toggle(field).catch(error => setStatus(String(error?.message || error)));
    } else beginEdit(field);
  });

  const display = (field: string): string => {
    if (field === "trial") return data?.stages?.trial_configured ? "Configured" : "Not configured";
    if (field === "formal") return data?.stages?.release_configured ? "Available" : "Release not configured";
    const value = valueFor(field);
    if (typeof value === "boolean") return value ? "ON" : "OFF";
    if (Array.isArray(value)) return `${value.length} rule(s)`;
    return String(value || "—");
  };

  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
    <HorizontalHeaderStrip items={[...TABS]} selected={TABS.findIndex(item => item.id === tab)} width={w}/>
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <Box flexDirection="column" paddingTop={1}>
      {!data ? <Text dimColor>Loading publish configuration…</Text> : rows.map((field, index) => <Box key={field}>
        <Text bold={index === row} dimColor={index !== row}>{LABELS[field]!.padEnd(20)}</Text>
        <Text bold={index === row}>{display(field)}</Text>
      </Box>)}
    </Box>
    {tab === "remote" && history.length > 0 ? <Box flexDirection="column" marginTop={1}>
      <Text dimColor>Publish activity</Text>
      {history.map((item, index) => <Text key={item.event_id || index} dimColor>
        {String(item.timestamp || "").slice(0, 19)}  {item.operation}  {item.status}
      </Text>)}
    </Box> : null}
    <Box flexGrow={1}/>
    <Text dimColor>{status || `${chordLabel("leftRight")} tabs   ${chordLabel("upDown")} select   ${chordLabel("enter")} open/change`}   {chordLabel("escape")} back</Text>
  </Box>;
});
