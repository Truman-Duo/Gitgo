import React, {memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {useModuleHelp} from "./ModuleHelpBoundary.js";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {archiveProject, configGet, configSet} from "../backend/tools.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {
  applyCommandAction, isCommandMode, resolveCommandKeys, type CommandHandlers,
} from "../input/commandInput.js";
import {chordLabel, matchChord} from "../input/bindings.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {colors, separator, usePanelSize} from "../theme/index.js";
import {SelectionRow, SelectionText} from "./Selection.js";
import {FormFieldRow, fieldLabelWidth} from "./FormFieldRow.js";
import {useCopy} from "../ui/PageFrame.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {cycleChoiceIndex} from "../input/choice.js";
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
  {id: "targets", label: {en:"Projects and Subprocesses",zh:"项目与子进程"}},
  {id: "plans", label: {en:"Deletion Plans",zh:"删除计划"}},
  {id: "delay", label: {en:"Retention and Delays",zh:"保留与延迟"}},
] as const;
const DELAYS = [0, 5, 10, 15, 60, 1440, 4320];
const DELAY_ROWS = [
  {field: "project", key: "delete_delay_minutes", label: {en:"Project deletion",zh:"项目删除"}},
  {field: "process", key: "process_delete_delay_minutes", label: {en:"Subprocess deletion",zh:"子进程删除"}},
  {field: "missing", key: "unavailable_project_archive_delay_minutes", label: {en:"Missing workspace archive",zh:"工作区缺失后归档"}},
] as const;

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
  const t=useCopy();
  const {w, h, contentH} = usePanelSize({minWidth: 40, widthOffset: 4, headerRows: 6, footerRows: 2});
  const [tab, setTab] = useState<TabId>("targets");
  const [index, setIndex] = useState(0);
  const [suggestion, setSuggestion] = useState(0);
  const [data, setData] = useState<any>({projects: [], processes: []});
  const [plans, setPlans] = useState<any[]>([]);
  const feedback = useFeedback({kind:"page",id:`bin:${tab}`});
  const status=feedback.text, setStatus=feedback.show;
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [confirmNo, setConfirmNo] = useState(true);
  const [delays, setDelays] = useState({project: 10, process: 10, missing: 60});
  const [delayRow, setDelayRow] = useState(0);
  const [editing, setEditing] = useState(false);
  const [delayDraft, setDelayDraft] = useState(10);
  const [delaySaving, setDelaySaving] = useState(false);
  const help=useModuleHelp(),menuHelp=useLocalHelpCommand("bin",onFooter,tab==="delay"&&!editing&&!confirmation);
  const scroll=useRef<ScrollBoxHandle>(null),selectedRow=useRef<DOMElement>(null);
  useScrollInput(scroll);
  useLayoutEffect(()=>{if(selectedRow.current)scroll.current?.scrollIntoView(selectedRow.current);},[index,delayRow,tab,w,h,data,plans]);

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
      missing: Number(value?.safety?.unavailable_project_archive_delay_minutes ?? 60),
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
    ? ["/help", "/restore", "/soft-delete", "/hard-delete"]
    : tab === "plans" ? ["/help", "/cancel", "/retry"] : [];
  const suggestions = useMemo<Suggestion[]>(() => cmdInput.value.startsWith("/")
    ? commands.filter(command => command.slice(1).startsWith(cmdInput.value.slice(1).toLowerCase()))
      .map(label => ({label, description: "", inputMode: "execute"}))
    : [], [cmdInput.value, commands.join("|")]);

  useEffect(() => {
    setIndex(value => Math.max(0, Math.min(value, Math.max(0, rows.length - 1))));
  }, [rows.length]);
  useEffect(() => {
    if(menuHelp.active)return;
    if (tab === "delay") onFooter({hidden: true});
    else onFooter({
      kind: "command", cmdInput,
      statusText: "",
      suggestions, suggestionIdx: suggestion, cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput, cmdInput.value, cmdInput.cursor, onFooter, status, suggestion, suggestions, tab, t,menuHelp.active]);

  const moveTab = (delta: number) => {
    const current = TABS.findIndex(item => item.id === tab);
    setTab(TABS[clampHeaderIndex(current, delta, TABS.length)]!.id);
    setIndex(0);
  };

  const prepare = useCallback(async (mode: "soft" | "hard") => {
    const target = targets[index];
    if (!target) { setStatus(t({en:"Nothing selected",zh:"尚未选择任何项目"})); return; }
    const args: any = {project: target.project, scope: target.scope, mode};
    if (target.scope === "process") args.process_id = target.process_id;
    const plan: any = await client.callTool("deletion.preview", args);
    const manifest = plan.manifest;
    const label = target.scope === "project" ? target.project : target.display_name || t({en:"Subprocess",zh:"子进程"});
    const cascade = target.scope === "project" && manifest.b_process_count
      ? t({en:`\nIncludes ${manifest.b_process_count} subprocess(es); ${manifest.unfinished_b_process_count || 0} unfinished.`,zh:`\n包含 ${manifest.b_process_count} 个子进程，其中 ${manifest.unfinished_b_process_count || 0} 个尚未完成。`})
      : "";
    setConfirmation({
      planId: plan.plan_id,
      message: t({en:`${mode.toUpperCase()} delete ${label}${cascade}\n${manifest.effect}\nDelay: ${manifest.delay_minutes} min; worktrees: ${manifest.worktrees.length}`,zh:`${mode === "soft" ? "软" : "永久"}删除 ${label}${cascade}\n${manifest.effect}\n延迟 ${manifest.delay_minutes} 分钟；工作树 ${manifest.worktrees.length} 个`}),
    });
    setConfirmNo(true);
  }, [client, index, targets, t, setStatus]);

  const runCommand = useCallback(async (raw: string) => {
    const command = raw.replace(/^[:/]\s*/, "").trim();
    if(command==="help"){cmdInput.setValue("");help?.open("bin");return;}
    setStatus("");
    try {
      const selected = rows[index];
      if (command === "restore") {
        if (selected?.scope === "project") await archiveProject(client, selected.project);
        else if (selected?.scope === "process") await client.callTool("process.archive", {
          project: selected.project, process_id: selected.process_id, archived: false,
        });
        else throw new Error(t({en:"Select an archived item",zh:"请选择已归档的项目"}));
      } else if (command === "soft-delete" || command === "hard-delete") {
        await prepare(command.startsWith("soft") ? "soft" : "hard");
      } else if (command === "cancel" && selected) {
        await client.callTool("deletion.cancel", {plan_id: selected.plan_id});
      } else if (command === "retry" && selected) {
        await client.callTool("deletion.retry", {plan_id: selected.plan_id});
      } else throw new Error(t({en:"Unknown command: ",zh:"未知命令："}) + command);
      if (!command.endsWith("delete")) { await refresh(); onRefresh?.(); }
    } catch (error: any) { setStatus(String(error?.message || error)); }
    cmdInput.setValue("");
  }, [client, cmdInput, index, onRefresh, prepare, refresh, rows, t, setStatus]);

  const handlers: CommandHandlers = {
    cmdInput, suggestionLabels: suggestions.map(item => item.label), suggestionIdx: suggestion,
    setSuggestionIdx: setSuggestion, runCommand,
  };

  useInput((input: string, key: any) => {
    if(menuHelp.handle(input,key))return;
    if (confirmation) {
      if (matchChord("escape", input, key)) setConfirmation(null);
      else if (matchChord("left", input, key) || matchChord("right", input, key)) {
        setConfirmNo(value => !value);
      } else if (matchChord("enter", input, key)) {
        if (confirmNo) { setConfirmation(null); return; }
        const planId = confirmation.planId;
        setConfirmation(null);
        setStatus(t({en:"Scheduling deletion…",zh:"正在安排删除…"}), {retention:{kind:"until-resolved",reason:"operation-pending"}});
        void client.callTool("deletion.confirm", {plan_id: planId}).then(async (plan: any) => {
          const result = plan.not_before * 1000 <= Date.now()
            ? await client.callTool("deletion.run", {plan_id: planId}, 120) : plan;
          setStatus(result.state === "completed" ? t({en:"Deletion completed",zh:"删除完成"})
            : t({en:`Deletion ${result.state}${result.error ? ": " + result.error : ""}`,zh:`删除状态：${result.state}${result.error ? "：" + result.error : ""}`}));
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
      else if (matchChord("down", input, key) && !editing) setDelayRow(value => Math.min(DELAY_ROWS.length - 1, value + 1));
      else if (matchChord("enter", input, key)) {
        if (delaySaving) return;
        if (!editing) { setDelayDraft(delays[DELAY_ROWS[delayRow]!.field]); setEditing(true); }
        else {
          const name = DELAY_ROWS[delayRow]!.key;
          const field = DELAY_ROWS[delayRow]!.field;
          const value = delayDraft;
          setDelaySaving(true);
          void configSet(client, "safety." + name, value)
            .then(() => { setDelays(current => ({...current, [field]: value})); setStatus(t({en:"Delay saved",zh:"延迟已保存"})); })
            .catch(error => setStatus(String(error?.message || error)))
            .finally(() => setDelaySaving(false));
          setEditing(false);
        }
      } else if (editing && (matchChord("left", input, key) || matchChord("right", input, key))) {
        const current = delayDraft;
        const position = Math.max(0, DELAYS.findIndex(value => value >= current));
        const delta = matchChord("left", input, key) ? -1 : 1;
        setDelayDraft(DELAYS[cycleChoiceIndex(position, delta, DELAYS.length)]!);
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
    <Text bold>{t({en:"Confirm deletion",zh:"确认删除"})}</Text>
    <Text>{confirmation.message}</Text>
    <Box marginTop={1}>
      <SelectionText selected={!confirmNo} variant="block">{t({en:"Delete",zh:"删除"})}</SelectionText>
      <Text>   </Text>
      <SelectionText selected={confirmNo} variant="block">{t({en:"Cancel",zh:"取消"})}</SelectionText>
    </Box>

  </Box>;

  const visibleCount = Math.max(1, contentH);
  const visibleStart = Math.max(0, Math.min(index - Math.floor(visibleCount / 2), rows.length - visibleCount));
  const labelWidth=fieldLabelWidth(DELAY_ROWS.map(item=>t(item.label)), Math.max(1,w-12), "");
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
    <HorizontalHeaderStrip items={TABS.map(item=>({id:item.id,label:t(item.label)}))} selected={TABS.findIndex(item => item.id === tab)} width={w}/>
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0} paddingTop={1}>
      {tab === "delay" ? <>{DELAY_ROWS.map((item, rowIndex) =>
        <Box key={item.key} ref={rowIndex === delayRow ? selectedRow : undefined} flexShrink={0}><FormFieldRow label={t(item.label)} selected={rowIndex === delayRow} editing={editing && rowIndex === delayRow}
          labelWidth={labelWidth} suffix="" highlight={false}>
          <SelectionText>{editing && rowIndex === delayRow ? delayDraft : delays[item.field]} {t({en:"min",zh:"分钟"})}</SelectionText>
        </FormFieldRow></Box>)}<Text dimColor>{t({en:"Missing workspaces: from first failed entry; 0 disables archiving. No files are deleted.",zh:"从首次打开工作区失败起计时；0 表示不归档。不会删除文件。"})}</Text></> : rows.length === 0 ? <Text dimColor>{t({en:"Nothing archived here.",zh:"此处暂无归档记录。"})}</Text>
        : rows.slice(visibleStart, visibleStart + visibleCount).map((item: any, offset: number) => {
          const absolute = visibleStart + offset;
          const label = tab === "targets"
            ? item.scope === "project" ? t({en:`Project   ${item.project}`,zh:`项目   ${item.project}`}) : t({en:`Subprocess   ${item.display_name || "—"} (${item.project})`,zh:`子进程   ${item.display_name || "—"} (${item.project})`})
            : `${item.manifest?.project || "?"} / ${item.manifest?.scope || "?"}   ${item.state}${item.error ? "   " + item.error : ""}`;
          return <SelectionRow key={item.plan_id || item.process_id || item.project} selected={absolute === index} ref={absolute === index ? selectedRow : undefined}>
            <SelectionText wrap="truncate-end">{label}</SelectionText>
          </SelectionRow>;
        })}
    </ScrollBox>
    {status?<Text dimColor>{status}</Text>:null}
  </Box>;
});
