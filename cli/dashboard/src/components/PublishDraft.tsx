import {usePublishFormEditor} from "../hooks/usePublishFormEditor.js";
import {useFeedback} from "../hooks/useFeedback.js";
import React from "react";
import {usePublishForm} from "../hooks/usePublishForm.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {Box, Text} from "@anthropic/ink";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishItem} from "../backend/publish.js";
import {formatChecks, parseChecks} from "./publishChecks.js";
import {parseContributions, parseSelection} from "./publishSelection.js";

const labels: Record<string, Copy> = {
  title: {en: "Semantic commit summary", zh: "正式提交的语义摘要"},
  source_role: {en: "Source role", zh: "来源角色"},
  source_ref: {en: "Source: HEAD, full ref or full commit ID", zh: "来源：HEAD、完整引用或完整提交号"},
  target_branch: {en: "New work branch", zh: "新工作分支"},
  owner: {en: "Person responsible", zh: "接手负责人"},
  rework: {en: "Remaining work (required for failed checks)", zh: "剩余工作（检查失败时必须填写）"},
  method: {en: "Method", zh: "方式"},
  contributions: {en: "Additional sources: role | ref; …", zh: "追加来源：角色 | 引用；多项用 ; 分隔"},
  selected_commits: {en: "Full commit IDs in replay order", zh: "按应用顺序填写完整提交号（空格分隔）"},
  paths: {en: "Selected changed file paths (; separated)", zh: "选中的变更文件路径（; 分隔）"},
  decision: {en: "Review: accept, rework or defer", zh: "审阅决定：accept 接纳、rework 返工、defer 暂缓"},
  note: {en: "Review notes", zh: "审阅说明"},
  test_target: {en: "Additional tests (; separated; blank inherits project checks)", zh: "追加测试（; 分隔；留空沿用项目检查）"},
};

export function PublishDraft({mode, item, busy, status, onFooter, onSubmit, onDismiss, localState, formKey = mode, initialValues}: {
  mode: "prepare" | "plan" | "revise" | "formal" | "combine" | "work" | "migrate" | "decide"; item?: PublishItem;
  busy: boolean; status: string;
  onFooter: (value: FooterConfig | null) => void;
  onSubmit: (args: Record<string, unknown>) => void; onDismiss: () => void;
  localState?: PublishLocalState; formKey?: string; initialValues?: Record<string, string>;
}) {
  const t = useCopy();
  const formal = mode === "formal" || mode === "combine" || mode === "migrate" || mode === "revise" && item?.plan?.destination === "formal";
  const previousChecks = item?.plan?.requested_checks ?? item?.plan?.checks ?? [];
  const defaults: Record<string, string> = {
    target_role: item?.plan?.target_role ?? "formal", base_oid: item?.plan?.target_absent ? item.plan.expected_target_oid ?? "" : "",
    title: item?.title ?? "",
    source_role: item?.candidate?.role ?? "workspace", source_ref: (mode === "work" ? item?.candidate?.oid : item?.candidate?.source_ref) ?? "HEAD",
    target_branch: mode === "work" ? "publish/repair" : item?.target?.replace(/^refs\/heads\//, "") ?? (formal ? "" : "publish/work"),
    owner: item?.owner ?? "", rework: item?.rework ?? "",
    decision: "defer", note: "",
    method: item?.plan?.method ?? "squash", test_target: item?.plan?.requested_checks !== undefined
      ? formatChecks(previousChecks) : item?.plan?.test_target ?? "",
    contributions: item?.plan?.contributions?.slice(1).map(row => `${row.role} | ${row.source_ref}`).join("; ") ?? "",
    selected_commits: item?.plan?.selected_commits?.join(" ") ?? "", paths: item?.plan?.paths?.join("; ") ?? "",
  };
  Object.assign(defaults, initialValues);
  const multiple = mode === "combine" || (item?.plan?.contributions?.length ?? 0) > 1;
  const form = usePublishForm(localState, formKey,
    defaults, JSON.stringify([item?.id, item?.revision, item?.binding, initialValues]), values => formal ? ["title", "source_role", "source_ref", ...(multiple ? ["contributions"] : []), "target_branch", "method",
      ...(["cherry_pick", "squash"].includes(values.method) ? ["selected_commits"] : values.method === "partial" ? ["paths"] : []), "owner", "test_target", "rework"]
    : mode === "plan" ? ["target_branch", "owner", "rework"]
    : mode === "decide" ? ["decision", "owner", "note"]
    : ["source_role", "source_ref", "target_branch", "owner", "rework"]);
  const feedback = useFeedback({kind: "page", id: `publish/draft/${formKey}/${mode}/${item?.id ?? "new"}`}, {tone: "error"});
  const error = feedback.text, setError = feedback.show;
  const {fields, stale} = form;
  const editor = usePublishFormEditor(form, {busy, onFooter, onDismiss,
    choices: {source_role: ["workspace", "trial"], method: ["squash", "ff", "merge", "rebase", "cherry_pick", "partial"], decision: ["accept", "rework", "defer"]},
    onSave: next => {
      try {
        const args = Object.fromEntries(fields.filter(name => !formal || !["test_target", "contributions", "selected_commits", "paths"].includes(name)).map(name => [name, next[name].trim()]));
        onSubmit({...args, ...(formal ? {destination: "formal", checks: parseChecks(next.test_target, previousChecks),
          ...(multiple ? {contributions: parseContributions(next.contributions)} : {}),
          ...(next.method === "squash" && next.selected_commits.trim() ? {selected_commits: next.selected_commits.trim().split(/\s+/)} : parseSelection(next.method, next.selected_commits, next.paths))} : mode === "work" ? {destination: "work"} : {}),
          ...(formal ? {target_role: next.target_role} : {}),
          ...(formal && next.base_oid ? {base_oid: next.base_oid} : {})});
        setError("");
      } catch { setError(t({en: "Check source syntax, full commit IDs, file paths and test limits.", zh: "请检查来源格式、完整提交号、文件路径及测试时限。"})); }
    }});
  return <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
    <Text bold>{t(mode === "decide" ? {en: "Review the fixed candidate", zh: "审阅固定候选"} : formal ? {en: "Prepare local Formal integration", zh: "准备本地正式整理"} : mode === "plan" ? {en: "Update adoption plan", zh: "调整接手计划"}
      : {en: "Prepare a fixed candidate", zh: "准备固定候选"})}</Text>

    {busy ? <Text>{t({en: "Preparing…", zh: "正在准备…"})}</Text> : null}
    {mode === "migrate" ? <Text>{t({en: "The old record is backed up. Supply its source and target explicitly; old sync/push flags do not verify them.",
      zh: "将备份旧记录。请明确填写来源与目标；旧同步/推送标志不作为验证结果。"})}</Text> : null}
    {mode === "work" ? <Text>{t({en: "The fixed source is adopted for repair. Recorded merge conflicts remain attached; describe the remaining work.",
      zh: "接手固定来源进行修复；保留已记录的合并冲突，请填写剩余工作。"})}</Text> : null}
    {status ? <Text>{status}</Text> : null}
    {stale ? <Text>{t({en: "This item changed since the draft. Ctrl+R starts again with the current item; Esc keeps the draft.", zh: "事项已变化，旧草稿不能直接保存。Ctrl+R 按当前事项重新填写；Esc 保留草稿返回。"})}</Text> : null}
    {localState?.error ? <Text>{t({en: "Local draft could not be saved. Keep this page open and retry editing.", zh: "本地草稿未能保存，请保留页面并重试编辑。"})}</Text> : null}
    {error ? <Text>{error}</Text> : null}
    {editor.rows(name => t(formal && name === "target_branch" ? {en: "Local formal branch", zh: "本地正式分支"} : labels[name]))}


  </Box>;
}
