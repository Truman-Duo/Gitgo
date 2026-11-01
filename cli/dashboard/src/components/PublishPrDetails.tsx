import {SelectionRow, SelectionText} from "./Selection.js";
import React, {useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {PublishItem, PublishPlatformSnapshot, PlatformRecord} from "../backend/publish.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import {parseUnifiedDiff} from "../utils/diff.js";
import {DiffView} from "./DiffView.js";

const clean = (value: string) => value.replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, " ");
/** One PR, three rows; drill-downs stay bound to that exact saved platform observation. */
export function PublishPrDetails({item, snapshot, document, onFooter, onDismiss, onCommand, localState, status}: {
  item?: PublishItem; snapshot: PublishPlatformSnapshot; document: PlatformRecord; localState: PublishLocalState; status?: string;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
  onCommand: (command: string, values?: Record<string, string>) => void;
}) {
  const t = useCopy(), {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [scene, setScene] = useState<"summary" | "files" | "discussion" | "handle" | "diff">("summary"), [row, setRow] = useState(0), [fileIndex, setFileIndex] = useState(0);
  const key = `pr/viewed/${snapshot.id}`, [viewed, setViewed] = useState<string[]>(() => localState.get<string[]>(key) ?? []);
  const scroll = useRef<ScrollBoxHandle>(null), selected = useRef<DOMElement>(null), file = snapshot.files?.[fileIndex];
  const full = snapshot.resource.kind === "pull" && String(snapshot.resource.number) === String(document.number ?? document.id);
  const number = String(document.number ?? document.id);
  const context = {kind: snapshot.target.kind, instance: snapshot.target.instance, repository: snapshot.target.repository, account: snapshot.target.account,
    resource: "pull", number, platform_snapshot_id: snapshot.id};
  const command = (name: string, values: Record<string, string> = {}) => onCommand(name, {...context, ...values});
  const rows = scene === "summary" ? [
    {label: t({en: "View changes", zh: "查看变更"}), run: () => {setScene("files"); setRow(0);}},
    {label: t({en: "Discussion and records", zh: "讨论与记录"}), run: () => {setScene("discussion"); setRow(0);}},
    {label: t({en: "Handle this PR", zh: "处理这个 PR"}), run: () => {setScene("handle"); setRow(0);}},
    {label: t({en: "Refresh this PR", zh: "刷新这个 PR"}), run: () => command("/remote")},
  ] : scene === "files" ? [
    ...(snapshot.files ?? []).map((entry, i) => ({label: `${viewed.includes(entry.path) ? "✓ " : "  "}${entry.path}${entry.unavailable ? " · " + t({en: "patch unavailable", zh: "差异不可用"}) : ""}`,
      run: () => {setFileIndex(i); setScene("diff"); setViewed(previous => [...new Set([...previous, entry.path])]);}})),
    {label: t({en: "Refresh file observations", zh: "刷新文件观察"}), run: () => command("/remote")},
    {label: t({en: "Prepare review of this PR", zh: "准备审查这个 PR"}), run: () => full ? command("/review") : command("/remote")},
  ] : scene === "discussion" ? [
    {label: t({en: "Prepare comment", zh: "准备评论"}), run: () => full ? command("/comment") : command("/remote")},
    ...(snapshot.comments ?? []).map(comment => ({label: `${comment.author?.login ?? ""}: ${comment.body ?? ""}`, run: () => command("/reply", {reference_kind: "comment", reference_id: comment.id})})),
    ...(snapshot.reviews ?? []).map(review => ({label: `${review.state ?? ""} · ${review.author?.login ?? ""}: ${review.body ?? ""}`, run: () => {}})),
    ...(snapshot.anchors ?? []).map(anchor => ({label: `${anchor.anchor?.path ?? anchor.anchor?.new_path ?? ""}: ${anchor.body ?? ""}`, run: () => command("/reply", {reference_kind: "anchor", reference_id: anchor.id})})),
  ] : scene === "handle" ? [
    {label: t({en: "Review checks and integration plan", zh: "核对检查与集成计划"}), run: () => onCommand("/flow")},
    {label: t({en: "Take over into Workspace", zh: "接手到 Workspace 继续完善"}), run: () => command("/work")},
    {label: t({en: "Prepare review / request changes", zh: "准备审查或要求修改"}), run: () => full ? command("/review") : command("/remote")},
    {label: t({en: "Record deferral", zh: "记录暂缓"}), run: () => command("/decide", {decision: "defer"})},
    {label: t({en: "Prepare close request", zh: "准备关闭请求"}), run: () => full ? command("/state", {state: "closed"}) : command("/remote")},
  ] : [];
  const difference = useMemo(() => !file?.patch ? [] : parseUnifiedDiff(`diff --git a/${clean(file.old_path || file.path)} b/${clean(file.path)}\n--- a/${clean(file.old_path || file.path)}\n+++ b/${clean(file.path)}\n${clean(file.patch)}`), [file]);
  useEffect(() => {onFooter({hidden: true}); return () => onFooter(null);}, [onFooter]);
  useEffect(() => {localState.set(key, viewed);}, [viewed, key]);
  useLayoutEffect(() => {if (selected.current) scroll.current?.scrollIntoView(selected.current); else scroll.current?.scrollTo(0);}, [scene, row, w]);
  useManagedInput((text, key) => {
    if (matchChord("escape", text, key)) {if (scene === "summary") onDismiss(); else {setScene(scene === "diff" ? "files" : "summary"); setRow(0);} return;}
    if (scene === "diff") {
      if (matchChord("up", text, key)) scroll.current?.scrollBy(-1);
      else if (matchChord("down", text, key)) scroll.current?.scrollBy(1);
      else if (text === "c") command("/review", {path: file?.path ?? ""});
      return;
    }
    if (matchChord("up", text, key)) setRow(v => Math.max(0, v - 1));
    else if (matchChord("down", text, key)) setRow(v => Math.min(rows.length - 1, v + 1));
    else if (matchChord("enter", text, key)) rows[row]?.run();
  });
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>#{clean(number)} {clean(document.title ?? "")} · {document.state ?? ""}</Text>
    {status ? <Text>{clean(status)}</Text> : null}
    <Text dimColor wrap="truncate-end">{snapshot.target.repository} · {document.refs?.head_sha?.slice(0, 12) ?? "—"} → {document.refs?.base_sha?.slice(0, 12) ?? "—"}</Text>
    {!full ? <Text>{t({en: "This is a saved list observation. Refresh this PR before drafting a review or inspecting its patch.", zh: "当前为已保存列表观察；审查或查看差异前请刷新这个 PR。"})}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {scene === "summary" ? <Text>{clean(document.body ?? "")}</Text> : null}
      {scene === "diff" && file ? <><Text>{clean(file.path)}</Text>{file.unavailable ? <Text>{t({en: "The platform did not provide a complete patch. No diff is inferred.", zh: "平台未提供完整差异，不推断缺失内容。"})}</Text> : <DiffView files={difference} width={w}/>}</> : null}
      {rows.map((entry, index) => <SelectionRow selected={index === row} key={index} ref={index === row ? selected : undefined} >
        <SelectionText selected={index === row}   wrap="wrap">{clean(entry.label)}</SelectionText>
      </SelectionRow>)}
      {snapshot.files_more || snapshot.comments_more || snapshot.reviews_more || snapshot.anchors_more ? <Text dimColor>{t({en: "Additional platform pages exist; refresh with the next page before treating this observation as complete.", zh: "平台还有后续页，请读取下一页后再判断信息是否完整。"})}</Text> : null}
    </ScrollBox>
    <Text dimColor>{scene === "diff" ? t({en: "↑/↓ scroll · c prepare anchored review · Esc files", zh: "↑/↓ 滚动 · c 准备行审查 · Esc 文件列表"}) : t({en: "↑/↓ select · Enter open · Esc back", zh: "↑/↓ 选择 · Enter 打开 · Esc 返回"})}</Text>
  </Box>;
}
