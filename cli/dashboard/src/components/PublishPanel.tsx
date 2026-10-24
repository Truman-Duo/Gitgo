import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import React, {memo, useCallback, useEffect, useMemo, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {publishGet, publishSet} from "../backend/tools.js";
import {applyTextOp} from "../hooks/useTextInput.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {colors, separator, usePanelSize, padEndWidth} from "../theme/index.js";
import type {FooterConfig} from "./CommandBar.js";
import {clampHeaderIndex, HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";

const TABS = [
  {id: "commit", label: {en:"Commit Format",zh:"提交格式"}},
  {id: "privacy", label: {en:"Privacy",zh:"隐私"}},
  {id: "remote", label: {en:"Remote",zh:"远端"}},
] as const;

const ROWS = {
  commit: ["prefix", "number_start", "padding", "plugins", "template_name"],
  privacy: [
    "enabled", "deep_scan", "content_level", "severity_threshold",
    "exclude_paths", "include_paths", "ignored_rules", "approved_fingerprints",
  ],
  remote: ["remote_url", "trial", "formal"],
} as const;

const LABELS: Record<string, Copy> = {
  prefix: {en:"Prefix",zh:"前缀"}, number_start: {en:"Number Start",zh:"起始编号"}, padding: {en:"Padding",zh:"编号补零"},
  plugins: {en:"Commit Plugins",zh:"提交插件"}, template_name: {en:"Template",zh:"模板"}, enabled: {en:"Privacy Scan",zh:"隐私扫描"},
  deep_scan: {en:"Deep Scan",zh:"深度扫描"}, content_level: {en:"Content Level",zh:"内容级别"},
  severity_threshold: {en:"Minimum Severity",zh:"最低严重级别"}, exclude_paths: {en:"Excluded Paths",zh:"排除路径"},
  include_paths: {en:"Explicit Includes",zh:"显式包含"}, ignored_rules: {en:"Ignored Rules",zh:"忽略规则"},
  approved_fingerprints: {en:"Approved Matches",zh:"已批准匹配"}, remote_url: {en:"Release URL",zh:"发布地址"},
  trial: {en:"Trial Changes",zh:"试用变更"}, formal: {en:"Formal Commits",zh:"正式提交"},
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

function parseEditedValue(field: string, raw: string,t:(value:Copy)=>string): unknown {
  if (ARRAY_FIELDS.has(field)) return raw.split(";").map(value => value.trim()).filter(Boolean);
  if (field === "number_start") {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) throw new Error(t({en:"Number Start must be a non-negative integer",zh:"起始编号必须为非负整数"}));
    return value;
  }
  if (field === "content_level") {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 1 || value > 3) throw new Error(t({en:"Content Level must be 1, 2 or 3",zh:"内容级别必须为 1、2 或 3"}));
    return value;
  }
  return raw;
}

export const PublishPanel = memo(function PublishPanel({
  client, project, cmdInput, onFooter, onDismiss, onOpenTrial, onOpenFormal,
}: Props) {
  const t=useCopy();
  const tabs=TABS.map(item=>({...item,label:t(item.label)}));
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
      statusText: t({en:"Enter save · Esc cancel",zh:"Enter 保存 · Esc 取消"}),
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
      const value = parseEditedValue(editing, cmdInput.value,t);
      if (tab === "commit") {
        await publishSet(client, project, "commit_format", {...data.commit_format, [editing]: value});
      } else if (tab === "privacy") {
        await publishSet(client, project, "privacy", {...data.privacy, [editing]: value});
      } else await publishSet(client, project, "remote_url", value);
      setEditing(null);
      cmdInput.setValue("");
      setStatus(t({en:"Saved",zh:"已保存"}));
      await refresh();
    } catch (error: any) { setStatus(String(error?.message || error)); }
  }, [client, cmdInput, data, editing, project, refresh, tab]);

  const toggle = useCallback(async (field: string) => {
    if (!data) return;
    const section = tab === "commit" ? "commit_format" : "privacy";
    const owner = tab === "commit" ? data.commit_format : data.privacy;
    await publishSet(client, project, section, {...owner, [field]: !owner[field]});
    setStatus(t({en:"Saved",zh:"已保存"}));
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
    if (field === "trial") return data?.stages?.trial_configured ? t({en:"Configured",zh:"已配置"}) : t({en:"Not configured",zh:"未配置"});
    if (field === "formal") return data?.stages?.release_configured ? t({en:"Available",zh:"可用"}) : t({en:"Release not configured",zh:"未配置发布仓库"});
    const value = valueFor(field);
    if (typeof value === "boolean") return value ? t({en:"ON",zh:"开启"}) : t({en:"OFF",zh:"关闭"});
    if (Array.isArray(value)) return t({en:`${value.length} items`,zh:`${value.length} 项`});
    return String(value || "—");
  };

  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
    <HorizontalHeaderStrip items={tabs} selected={TABS.findIndex(item => item.id === tab)} width={w}/>
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <Box flexDirection="column" paddingTop={1}>
      {!data ? <Text dimColor>{t({en:"Loading publish configuration…",zh:"正在读取发布配置…"})}</Text> : rows.map((field, index) => <Box key={field}>
        <Text bold={index === row} dimColor={index !== row}>{padEndWidth(t(LABELS[field]!),20)}</Text>
        <Text bold={index === row}>{display(field)}</Text>
      </Box>)}
    </Box>
    {tab === "remote" && history.length > 0 ? <Box flexDirection="column" marginTop={1}>
      <Text dimColor>{t({en:"Publish activity",zh:"发布活动"})}</Text>
      {history.map((item, index) => <Text key={item.event_id || index} dimColor>
        {String(item.timestamp || "").slice(0, 19)}  {activityName(String(item.operation),t)}  {activityName(String(item.status),t)}
      </Text>)}
    </Box> : null}
    <Box flexGrow={1}/>
    <Text dimColor>{status || t({en:"←/→ tabs · ↑/↓ select · Enter open/change",zh:"←/→ 页签 · ↑/↓ 选择 · Enter 打开/修改"})} · {t({en:"Esc back",zh:"Esc 返回"})}</Text>
  </Box>;
});


function activityName(value:string,t:(copy:Copy)=>string):string{
 const names:Record<string,Copy>={
  scan:{en:"Scan",zh:"扫描"},trial:{en:"Trial",zh:"试用"},formalize:{en:"Formalize",zh:"正式化"},
  sync:{en:"Sync",zh:"同步"},push:{en:"Push",zh:"推送"},success:{en:"Success",zh:"成功"},error:{en:"Error",zh:"错误"},
  failed:{en:"Failed",zh:"失败"},info:{en:"Info",zh:"信息"},
  governance_synced:{en:"Synced",zh:"已同步"},governance_pushed:{en:"Pushed",zh:"已推送"},governance_dissolved:{en:"Dissolved",zh:"已拆解"},
 };
 return names[value]?t(names[value]!):value;
}
