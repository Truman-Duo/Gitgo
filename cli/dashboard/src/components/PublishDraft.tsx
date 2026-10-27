import React, {useEffect, useState} from "react";
import {usePublishForm} from "../hooks/usePublishForm.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {Box, Text} from "@anthropic/ink";
import {applyTextOp} from "../hooks/useTextInput.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishItem} from "../backend/publish.js";
import {formatChecks, parseChecks} from "./publishChecks.js";
import {parseContributions, parseSelection} from "./publishSelection.js";

const labels: Record<string, Copy> = {
  source_role: {en: "Source role: workspace or trial", zh: "来源角色：workspace 或 trial"},
  source_ref: {en: "Source: HEAD, full ref or full commit ID", zh: "来源：HEAD、完整引用或完整提交号"},
  target_branch: {en: "New work branch", zh: "新工作分支"},
  owner: {en: "Person responsible", zh: "接手负责人"},
  rework: {en: "Remaining work (required for failed checks)", zh: "剩余工作（检查失败时必须填写）"},
  method: {en: "Method: squash, ff, merge, rebase, cherry_pick, partial", zh: "方式：squash、ff、merge、rebase、cherry_pick、partial"},
  contributions: {en: "Additional sources: role | ref; …", zh: "追加来源：角色 | 引用；多项用 ; 分隔"},
  selected_commits: {en: "Full commit IDs in replay order", zh: "按应用顺序填写完整提交号（空格分隔）"},
  paths: {en: "Selected changed file paths (; separated)", zh: "选中的变更文件路径（; 分隔）"},
  decision: {en: "Review: accept, rework or defer", zh: "审阅决定：accept 接纳、rework 返工、defer 暂缓"},
  note: {en: "Review notes", zh: "审阅说明"},
  test_target: {en: "Additional tests (; separated; blank inherits project checks)", zh: "追加测试（; 分隔；留空沿用项目检查）"},
};

export function PublishDraft({mode, item, busy, status, onFooter, onSubmit, onDismiss, localState, formKey = mode}: {
  mode: "prepare" | "plan" | "revise" | "formal" | "combine" | "work" | "migrate" | "decide"; item?: PublishItem;
  busy: boolean; status: string;
  onFooter: (value: FooterConfig | null) => void;
  onSubmit: (args: Record<string, unknown>) => void; onDismiss: () => void;
  localState?: PublishLocalState; formKey?: string;
}) {
  const t = useCopy();
  const formal = mode === "formal" || mode === "combine" || mode === "migrate" || mode === "revise" && item?.plan?.destination === "formal";
  const previousChecks = item?.plan?.requested_checks ?? item?.plan?.checks ?? [];
  const defaults: Record<string, string> = {
    source_role: item?.candidate?.role ?? "workspace", source_ref: (mode === "work" ? item?.candidate?.oid : item?.candidate?.source_ref) ?? "HEAD",
    target_branch: mode === "work" ? "publish/repair" : item?.target?.replace(/^refs\/heads\//, "") ?? (formal ? "" : "publish/work"),
    owner: item?.owner ?? "", rework: item?.rework ?? "",
    decision: "defer", note: "",
    method: item?.plan?.method ?? "squash", test_target: item?.plan?.requested_checks !== undefined
      ? formatChecks(previousChecks) : item?.plan?.test_target ?? "",
    contributions: item?.plan?.contributions?.slice(1).map(row => `${row.role} | ${row.source_ref}`).join("; ") ?? "",
    selected_commits: item?.plan?.selected_commits?.join(" ") ?? "", paths: item?.plan?.paths?.join("; ") ?? "",
  };
  const multiple = mode === "combine" || (item?.plan?.contributions?.length ?? 0) > 1;
  const {values, setValues, fields, index, setIndex, field, input, stale, reset} = usePublishForm(localState, formKey,
    defaults, JSON.stringify([item?.id, item?.revision, item?.binding]), values => formal ? ["source_role", "source_ref", ...(multiple ? ["contributions"] : []), "target_branch", "method",
      ...(values.method === "cherry_pick" ? ["selected_commits"] : values.method === "partial" ? ["paths"] : []), "owner", "test_target", "rework"]
    : mode === "plan" ? ["target_branch", "owner", "rework"]
    : mode === "decide" ? ["decision", "owner", "note"]
    : ["source_role", "source_ref", "target_branch", "owner", "rework"]);
  const [error, setError] = useState("");
  useEffect(() => {
    onFooter({kind: "normal", cmdInput: input, statusText: t({en: "↑/↓ field · Enter next/save · Esc cancel", zh: "↑/↓ 字段 · Enter 下一项/保存 · Esc 取消"}),
      suggestions: [], suggestionIdx: 0, cmdResult: ""});
    return () => onFooter(null);
  }, [input, input.value, input.cursor, onFooter, t]);
  useInput((text, key) => {
    if (busy) return;
    if (key.ctrl && text === "r") { reset(); return; }
    if (matchChord("escape", text, key)) { onDismiss(); return; }
    if (stale && matchChord("enter", text, key)) return;
    const move = matchChord("up", text, key) ? -1 : matchChord("down", text, key) ? 1 : 0;
    if (move) {
      setValues({...values, [field]: input.materialize()});
      setIndex(Math.max(0, Math.min(fields.length - 1, index + move)));
      return;
    }
    if (matchChord("enter", text, key)) {
      const value = input.materialize().trim();
      if (!["rework", "test_target", "contributions"].includes(field) && !value) return;
      if (field === "source_role" && !["workspace", "trial"].includes(value)) return;
      if (field === "method" && !["squash", "ff", "merge", "rebase", "cherry_pick", "partial"].includes(value)) return;
      if (field === "decision" && !["accept", "rework", "defer"].includes(value)) return;
      const next = {...values, [field]: value}; setValues(next);
      if (index < fields.length - 1) setIndex(index + 1);
      else {
        try {
          const args = Object.fromEntries(fields.filter(name => !formal || !["test_target", "contributions", "selected_commits", "paths"].includes(name)).map(name => [name, next[name]]));
          onSubmit({...args, ...(formal ? {destination: "formal", checks: parseChecks(next.test_target, previousChecks),
            ...(multiple ? {contributions: parseContributions(next.contributions)} : {}),
            ...parseSelection(next.method, next.selected_commits, next.paths)} : mode === "work" ? {destination: "work"} : {})});
          setError("");
        } catch { setError(t({en: "Check source syntax, full commit IDs, file paths and test limits.", zh: "请检查来源格式、完整提交号、文件路径及测试时限。"})); }
      }
      return;
    }
    if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
    else if (matchChord("delete", text, key)) applyTextOp({op: "delete_forward"}, input);
    else if (matchChord("left", text, key)) applyTextOp({op: "move_cursor", delta: -1}, input);
    else if (matchChord("right", text, key)) applyTextOp({op: "move_cursor", delta: 1}, input);
    else if (matchChord("home", text, key)) applyTextOp({op: "move_to_start"}, input);
    else if (matchChord("end", text, key)) applyTextOp({op: "move_to_end"}, input);
    else if (key.ctrl && text === "u") input.setValue("");
    else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
  });
  return <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
    <Text bold>{t(mode === "decide" ? {en: "Review the fixed candidate", zh: "审阅固定候选"} : formal ? {en: "Prepare local Formal integration", zh: "准备本地正式整理"} : mode === "plan" ? {en: "Update adoption plan", zh: "调整接手计划"}
      : {en: "Prepare a fixed candidate", zh: "准备固定候选"})}</Text>
    <Text>{t(mode === "decide" ? {en: "A review records responsibility and intent. It does not turn failed checks into passes or execute Git.", zh: "审阅记录责任与意图，不会把失败检查改成通过，也不执行 Git。"} : formal ? {en: "Choose an existing Formal branch. Review the result, run /verify, then /execute.", zh: "填写已有的 Formal 分支。保存后查看结果，/verify 检查，再 /execute 合入。"}
      : {en: "Saving records the candidate and checks. Use /adopt afterward to create the branch.", zh: "保存时固定候选并记录检查；之后用 /adopt 创建工作分支。"})}</Text>
    {busy ? <Text>{t({en: "Preparing…", zh: "正在准备…"})}</Text> : null}
    {mode === "migrate" ? <Text>{t({en: "The old record is backed up. Supply its source and target explicitly; old sync/push flags do not verify them.",
      zh: "将备份旧记录。请明确填写来源与目标；旧同步/推送标志不作为验证结果。"})}</Text> : null}
    {mode === "work" ? <Text>{t({en: "The fixed source is adopted for repair. Recorded merge conflicts remain attached; describe the remaining work.",
      zh: "接手固定来源进行修复；保留已记录的合并冲突，请填写剩余工作。"})}</Text> : null}
    {status ? <Text>{status}</Text> : null}
    {stale ? <Text>{t({en: "This item changed since the draft. Ctrl+R starts again with the current item; Esc keeps the draft.", zh: "事项已变化，旧草稿不能直接保存。Ctrl+R 按当前事项重新填写；Esc 保留草稿返回。"})}</Text> : null}
    {localState?.error ? <Text>{t({en: "Local draft could not be saved. Keep this page open and retry editing.", zh: "本地草稿未能保存，请保留页面并重试编辑。"})}</Text> : null}
    {error ? <Text>{error}</Text> : null}
    {fields.map((name, i) => <Text key={name} bold={i === index} wrap="truncate-end">
      {i === index ? "› " : "  "}{t(formal && name === "target_branch" ? {en: "Existing Formal branch", zh: "已有 Formal 分支"} : labels[name])}: {(i === index ? input.value : values[name]).replace(/[\x00-\x1f\x7f-\x9f]/g, " ")}
    </Text>)}
    {formal ? <Text dimColor>{t({en: "Tests: file.py | 42,7 | 30; other.py | 42 | 60. Each entry is required.",
      zh: "测试示例：file.py | 42,7 | 30; other.py | 42 | 60。每项均需通过。"})}</Text> : null}
    <Text dimColor>{t({en: "Changing the target invalidates existing evidence. Preparing a revision preserves earlier evidence.", zh: "更换目标后原证据失效；重新准备会保留以前的证据。"})}</Text>
  </Box>;
}
