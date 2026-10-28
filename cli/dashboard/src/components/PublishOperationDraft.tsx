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
import {usePanelSize} from "../theme/index.js";

export type PublishOperationMode = "remote" | "search" | "edit" | "state" | "comment_edit" | "reply" | "followup" | "comment" | "review" | "issue" | "pull" | "account" | "package" | "export" | "release" | "tag" | "import" | "download" | "link" | "branch";
const labels: Record<string, Copy> = {
  kind: {en: "Platform: github or gitlab", zh: "平台：github 或 gitlab"}, instance: {en: "HTTPS instance", zh: "HTTPS 实例地址"},
  repository: {en: "Repository: owner/name", zh: "仓库：所有者/名称"}, account: {en: "Account login", zh: "账户登录名"},
  resource: {en: "Read: issue, pull, issues or pulls", zh: "读取对象：issue、pull、issues 或 pulls"}, number: {en: "Issue / PR / MR number", zh: "Issue / PR / MR 编号"},
  token: {en: "Access token (stored encrypted)", zh: "访问令牌（加密保存）"}, page: {en: "Page (1–20)", zh: "页码（1–20）"},
  body: {en: "Draft text", zh: "草稿正文"}, title: {en: "Title", zh: "标题"},
  decision: {en: "Review: COMMENT, APPROVE, REQUEST_CHANGES", zh: "审查：COMMENT、APPROVE、REQUEST_CHANGES"},
  path: {en: "Anchor file (blank for overall review)", zh: "锚点文件（整体审查留空）"}, line: {en: "Anchor line", zh: "锚点行号"},
  side: {en: "Anchor side: LEFT or RIGHT", zh: "锚点侧：LEFT 或 RIGHT"},
  head: {en: "Source branch", zh: "来源分支"}, base: {en: "Target branch", zh: "目标分支"},
  head_oid: {en: "Fixed source commit", zh: "固定来源提交号"}, base_oid: {en: "Expected target commit", zh: "预期目标提交号"},
  version: {en: "Version", zh: "版本号"}, entry: {en: "Build script (blank for source archive)", zh: "构建脚本（仅源码包时留空）"},
  outputs: {en: "Build output files (; separated)", zh: "构建输出文件（; 分隔）"}, timeout: {en: "Build timeout in seconds", zh: "构建时限（秒）"},
  package_id: {en: "Package ID", zh: "产物记录 ID"}, destination: {en: "Existing export directory", zh: "已有导出目录"},
  remote: {en: "Named remote (blank for local tag)", zh: "远端名称（仅本地标签时留空）"},
  package_path: {en: "Absolute package file path", zh: "产物文件的绝对路径"}, sha256: {en: "Expected SHA-256", zh: "预期 SHA-256"},
  process_id: {en: "Existing process ID", zh: "已有进程 ID"}, parent_process_id: {en: "Parent process ID (optional)", zh: "父进程 ID（可选）"},
  decision_id: {en: "Review decision ID (optional)", zh: "审阅决定 ID（可选）"}, snapshot_id: {en: "Platform snapshot ID (optional)", zh: "平台观察 ID（可选）"},
  anchor_id: {en: "Platform anchor ID (optional)", zh: "平台锚点 ID（可选）"}, note: {en: "Link note (optional)", zh: "关联说明（可选）"},
  action: {en: "Branch action", zh: "分支操作"}, branch: {en: "Owned work branch", zh: "本事项创建的工作分支"},
  owner: {en: "Responsible person", zh: "负责人"}, reason: {en: "Retention / change reason", zh: "保留或变更原因"},
  remote_branch: {en: "Remote branch (blank to use work branch)", zh: "远端分支（留空沿用工作分支）"},
  merge_item_id: {en: "Accepted merge item ID (optional)", zh: "已接纳的集成事项 ID（可选）"},
  query: {en: "Search text in this repository", zh: "当前仓库的搜索文本"}, state: {en: "Desired state: open or closed", zh: "目标状态：open 或 closed"},
  reference_kind: {en: "Reply / edit target: comment or anchor", zh: "回复或编辑对象：comment 或 anchor"},
  reference_id: {en: "Saved comment / thread ID", zh: "已保存的评论或线程 ID"},
  followup_status: {en: "Follow-up: pending_reply or resolved", zh: "跟进状态：pending_reply 待回复或 resolved 已处理"},
};

export function PublishOperationDraft({mode, item, busy, status, onFooter, onSubmit, onDismiss, localState, formKey = mode}: {
  mode: PublishOperationMode; item?: PublishItem; busy: boolean; status: string;
  onFooter: (value: FooterConfig | null) => void; onSubmit: (operation: string, args: Record<string, unknown>) => void; onDismiss: () => void;
  localState?: PublishLocalState; formKey?: string;
}) {
  const t = useCopy(), {h, w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const snapshot = item?.collaboration?.snapshots.at(-1);
  const target = snapshot?.target;
  const defaults: Record<string, string> = {kind: target?.kind ?? "github", instance: target?.instance ?? "https://github.com",
    repository: target?.repository ?? "", account: target?.account ?? "", resource: mode === "search" ? "issues" : snapshot?.resource.kind ?? "issue",
    number: String(snapshot?.resource.number ?? ""), page: "1", token: "", body: mode === "edit" ? snapshot?.document?.body ?? "" : "", title: mode === "edit" ? snapshot?.document?.title ?? "" : "", decision: "COMMENT", path: "", line: "", side: "RIGHT",
    head: "", base: "", head_oid: item?.candidate_oid ?? "", base_oid: item?.plan?.expected_target_oid ?? "",
    version: "", entry: "", outputs: "", timeout: "120", package_id: item?.packages?.at(-1)?.id ?? "", destination: "", remote: "",
    package_path: "", sha256: "", process_id: "", parent_process_id: "", decision_id: "", snapshot_id: "", anchor_id: "", note: "",
    action: "retain_source", branch: item?.target ?? "", owner: item?.owner ?? "", reason: "", remote_branch: "", merge_item_id: "",
    query: "", state: "closed", reference_kind: "comment", reference_id: "", followup_status: "pending_reply"};
  const platform = ["kind", "instance", "repository", "account"];
  const {values, setValues, fields, index, setIndex, field, input, stale, reset} = usePublishForm(localState, formKey,
    defaults, JSON.stringify([item?.id, item?.revision, item?.binding, snapshot?.id]), values => mode === "remote" ? [...platform, "resource", ...(["issue", "pull"].includes(values.resource) ? ["number"] : []), "page"]
    : mode === "search" ? [...platform, "resource", "query", "page"]
    : mode === "edit" ? ["title", "body"] : mode === "state" ? ["state"]
    : ["comment_edit", "reply"].includes(mode) ? ["reference_kind", "reference_id", "body"]
    : mode === "followup" ? ["reference_kind", "reference_id", "followup_status", "owner", "note"]
    : mode === "account" ? [...platform, "token"] : mode === "release" ? [...platform, "package_id"]
    : mode === "download" ? [...platform, "package_id", "destination"]
    : mode === "tag" ? ["package_id", "remote"] : mode === "import" ? ["package_path", "sha256"]
    : mode === "link" ? ["process_id", "parent_process_id", "decision_id", "snapshot_id", "anchor_id", "note"]
    : mode === "branch" ? ["action", ...(values.action === "retain_source" ? [] : ["branch"]), "owner", "reason",
      ...(["prepare_publish", "prepare_remote_delete"].includes(values.action) ? ["remote", "remote_branch"] : []),
      ...(values.action.startsWith("prepare_") ? ["merge_item_id"] : [])]
    : mode === "package" ? ["version", "entry", ...(values.entry ? ["outputs", "timeout"] : [])]
    : mode === "export" ? ["package_id", "destination"]
    : mode === "review" ? ["decision", "path", ...(values.path ? ["line", "side"] : []), "body"]
    : mode === "pull" ? ["title", "head", "base", "head_oid", "base_oid", "body"]
    : mode === "issue" ? ["title", "body"] : ["body"]);
  const [error, setError] = useState("");
  useEffect(() => {
    onFooter({kind: "normal", cmdInput: field === "token" ? {...input, value: "•".repeat(input.value.length)} : input,
      statusText: t(w < 60 ? {en: "↑/↓ · Enter next/save · Esc back", zh: "↑/↓ · Enter继续 · Esc返回"}
        : {en: "↑/↓ field · Enter next/save · Esc keeps draft", zh: "↑/↓ 字段 · Enter 下一项/保存 · Esc 保留返回"}), suggestions: [], suggestionIdx: 0, cmdResult: ""});
    return () => onFooter(null);
  }, [input, input.value, input.cursor, field, onFooter, t, w]);
  const submit = (next: Record<string, string>) => {
    const selectedTarget = Object.fromEntries(platform.map(name => [name, next[name]]));
    if (mode === "account") return onSubmit("account.save", {target: selectedTarget, token: next.token});
    if (mode === "remote") return onSubmit("collaborate", {action: "refresh", target: selectedTarget,
      resource: {kind: next.resource, ...(["issue", "pull"].includes(next.resource) ? {number: Number(next.number)} : {})}, page: Number(next.page)});
    if (mode === "search") return onSubmit("collaborate", {action: "search", target: selectedTarget, resource: {kind: next.resource}, query: next.query, page: Number(next.page)});
    if (mode === "package") return onSubmit("package", {version: next.version, ...(next.entry ? {recipe: {entry: next.entry,
      outputs: next.outputs.split(";").map(value => value.trim()).filter(Boolean), timeout: Number(next.timeout)}} : {})});
    if (mode === "export") return onSubmit("export", {package_id: next.package_id, destination: next.destination});
    if (mode === "release") return onSubmit("release", {package_id: next.package_id, target: selectedTarget});
    if (mode === "download") return onSubmit("download", {package_id: next.package_id, target: selectedTarget, destination: next.destination});
    if (mode === "tag") return onSubmit("tag", {package_id: next.package_id, remote: next.remote});
    if (mode === "import") return onSubmit("import_package", {path: next.package_path, sha256: next.sha256});
    if (mode === "link") return onSubmit("link_task", Object.fromEntries(fields.map(name => [name, next[name]])));
    if (mode === "branch") return onSubmit("branch", Object.fromEntries(fields.map(name => [name, next[name]])));
    if (!snapshot || snapshot.binding !== item?.binding) { setError(t({en: "Refresh this platform object first with /remote.", zh: "请先用 /remote 刷新当前平台对象。"})); return; }
    const reference = {kind: next.reference_kind, id: next.reference_id};
    if (mode === "followup") return onSubmit("collaborate", {action: "followup", snapshot_id: snapshot.id,
      followup: {reference, status: next.followup_status, owner: next.owner, note: next.note}});
    const content: Record<string, unknown> = {kind: mode, body: next.body};
    if (["issue", "pull", "edit"].includes(mode)) content.title = next.title;
    if (mode === "state") { content.state = next.state; content.body = ""; }
    if (["comment_edit", "reply"].includes(mode)) content.reference = reference;
    if (mode === "pull") for (const key of ["head", "base", "head_oid", "base_oid"]) content[key] = next[key];
    if (mode === "review") {
      content.decision = next.decision;
      if (next.path) content.anchor = {path: next.path, line: Number(next.line), side: next.side, commit_id: snapshot.document?.refs?.head_sha};
    }
    onSubmit("collaborate", {action: "draft", snapshot_id: snapshot.id, draft: content});
  };
  useInput((text, key) => {
    if (busy) return;
    if (key.ctrl && text === "r") { reset(); return; }
    if (matchChord("escape", text, key)) { onDismiss(); return; }
    if (stale && matchChord("enter", text, key)) return;
    const move = matchChord("up", text, key) ? -1 : matchChord("down", text, key) ? 1 : 0;
    if (move) { setValues({...values, [field]: input.materialize()}); setIndex(Math.max(0, Math.min(fields.length - 1, index + move))); return; }
    if (matchChord("enter", text, key)) {
      const value = input.materialize().trim();
      if (!value && !["path", "entry", "remote", "parent_process_id", "decision_id", "snapshot_id", "anchor_id", "note", "remote_branch", "merge_item_id"].includes(field)) return;
      if (field === "action" && !["retain_source", "retain_work", "prepare_archive", "prepare_delete", "prepare_publish", "prepare_remote_delete"].includes(value)) return;
      if (field === "kind" && !["github", "gitlab"].includes(value) || field === "resource" && !["issue", "pull", "issues", "pulls"].includes(value)) return;
      if (mode === "search" && field === "resource" && !["issues", "pulls"].includes(value)) return;
      if (field === "state" && !["open", "closed"].includes(value) || field === "reference_kind" && !["comment", "anchor"].includes(value)
        || field === "followup_status" && !["pending_reply", "resolved"].includes(value)) return;
      if (field === "side" && !["LEFT", "RIGHT"].includes(value)) return;
      if (["line", "page", "timeout", "number"].includes(field) && (!Number.isInteger(Number(value)) || Number(value) < 1)) return;
      const next = {...values, [field]: value}; setValues(next);
      // Build fields grow when the script is selected at the old last field.
      if (mode === "package" && field === "entry" && !value) submit(next);
      else if (mode === "package" && field === "entry" && value || index < fields.length - 1) setIndex(index + 1);
      else submit(next);
    } else if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
    else if (matchChord("delete", text, key)) applyTextOp({op: "delete_forward"}, input);
    else if (matchChord("left", text, key)) applyTextOp({op: "move_cursor", delta: -1}, input);
    else if (matchChord("right", text, key)) applyTextOp({op: "move_cursor", delta: 1}, input);
    else if (matchChord("home", text, key)) applyTextOp({op: "move_to_start"}, input);
    else if (matchChord("end", text, key)) applyTextOp({op: "move_to_end"}, input);
    else if (key.ctrl && text === "u") input.setValue("");
    else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
  });
  const visible = Math.max(1, h - (mode === "branch" ? 10 : 7));
  const start = Math.max(0, Math.min(index - Math.floor(visible / 2), fields.length - visible));
  return <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
    <Text bold>{t({en: "Publish · ", zh: "Publish · "})}{mode} · {index + 1}/{fields.length}</Text>
    {mode === "branch" ? <Text>{t({en: "retain_source / retain_work record retention; prepare_archive / prepare_delete / prepare_publish / prepare_remote_delete prepare a change. Review it, then /branch-apply.", zh: "retain_source / retain_work 记录保留；prepare_archive / prepare_delete / prepare_publish / prepare_remote_delete 准备归档、删除或发布。核对后 /branch-apply 执行。"})}</Text> : null}
    <Text>{t(["comment", "review", "issue", "pull", "edit", "state", "comment_edit", "reply"].includes(mode)
      ? {en: "Save the draft, review it, then explicitly /send its ID.", zh: "先保存草稿并检查内容，再明确 /send 草稿 ID。"}
      : mode === "account" ? {en: "This account is scoped to this platform repository. The token is stored in the OS encrypted store.", zh: "账户绑定此平台仓库；令牌使用操作系统加密保存。"}
      : {en: "Review the fixed target and inputs before saving.", zh: "保存前核对固定目标和输入。"})}</Text>
    {status || error ? <Text>{status || error}</Text> : null}
    {stale ? <Text>{t({en: "This item changed since the draft. Ctrl+R starts again; Esc keeps the draft.", zh: "事项已变化，旧草稿不能直接保存。Ctrl+R 重新填写；Esc 保留草稿返回。"})}</Text> : null}
    {localState?.error ? <Text>{t({en: "Local draft could not be saved.", zh: "本地草稿未能保存，请保留页面并重试编辑。"})}</Text> : null}
    {fields.slice(start, start + visible).map((name, offset) => { const i = start + offset; return <Text key={name} bold={i === index} wrap="truncate-end">{i === index ? "› " : "  "}{t(labels[name])}: {name === "token" ? "•".repeat((i === index ? input.value : values[name]).length) : (i === index ? input.value : values[name]).replace(/[\x00-\x1f\x7f-\x9f]/g, " ")}</Text>; })}
  </Box>;
}
