import React, {useEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useTextInput, applyTextOp} from "../hooks/useTextInput.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";

type Commit = {oid: string; parents: string[]; subject: string; author: {name: string; email: string; timestamp: number}};
type History = {snapshot_id: string; head_oid: string; relation: string; commits: Commit[]; next_cursor: string | null};
type Difference = {text: string; truncated?: boolean; base_oid: string; head_oid: string};
const safe = (text: string) => text.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, " ");

export function PublishHistoryPanel({client, project, itemId, head, onFooter, onDismiss}: {
  client: BackendClient; project: string; itemId: string; head: string;
  onFooter: (footer: FooterConfig | null) => void; onDismiss: () => void;
}) {
  const t = useCopy(), input = useTextInput(), {h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [page, setPage] = useState<History | null>(null), [selected, setSelected] = useState(0);
  const [diff, setDiff] = useState<Difference | null>(null), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const generation = useRef(0), scroll = useRef<ScrollBoxHandle>(null), pages = useRef<{page: History; selected: number}[]>([]);
  const read = async (cursor = "") => {
    const current = ++generation.current; setBusy(true); setError("");
    try {
      const result = await client.callTool("publish.history", {project, item_id: itemId, head, limit: 50, cursor}) as History;
      if (current !== generation.current) return;
      if (result.relation !== "git_parent" || !Array.isArray(result.commits)) throw new Error("PUBLISH_RESPONSE_MISMATCH");
      if (cursor && page) pages.current.push({page, selected});
      setPage(result); setSelected(0); setDiff(null);
    } catch (failure: any) { if (current === generation.current) setError(String(failure?.code || failure?.message)); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const readDiff = async (parent = 0) => {
    const commit = page?.commits[selected];
    if (!commit || parent < 0 || parent >= Math.max(1, commit.parents.length)) return;
    const current = ++generation.current; setBusy(true); setError("");
    try {
      const result = await client.callTool("publish.diff", {project, item_id: itemId, head, snapshot_id: page!.snapshot_id,
        base_oid: commit.parents[parent] ?? "", head_oid: commit.oid, limit_bytes: 262144}) as Difference;
      if (current !== generation.current) return;
      setDiff(result); scroll.current?.scrollTo(0);
    } catch (failure: any) { if (current === generation.current) setError(String(failure?.code || failure?.message)); }
    finally { if (current === generation.current) setBusy(false); }
  };
  useEffect(() => { void read(); return () => { generation.current++; }; }, [project, itemId, head]);
  useEffect(() => {
    onFooter({kind: "command", cmdInput: input, suggestions: [], suggestionIdx: 0, cmdResult: "",
      statusText: t({en: "↑/↓ commit · Enter diff · /more · /parent N · Esc back", zh: "↑/↓ 提交 · Enter 差异 · /more 下一页 · /parent N 指定父提交 · Esc 返回"})});
    return () => onFooter(null);
  }, [input, input.value, input.cursor, onFooter, t]);
  useInput((text, key) => {
    if (input.value.startsWith("/")) {
      if (matchChord("escape", text, key)) input.setValue("");
      else if (matchChord("enter", text, key)) {
        const command = input.value.trim(); input.setValue("");
        if (busy) return;
        if (command === "/more" && page?.next_cursor) void read(page.next_cursor);
        else if (command === "/refresh") { pages.current = []; void read(); }
        else if (/^\/parent [1-9]\d*$/.test(command)) void readDiff(Number(command.split(" ")[1]) - 1);
      } else if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
      else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
      return;
    }
    if (text === "/") { input.setValue("/"); return; }
    if (matchChord("escape", text, key)) {
      generation.current++; setBusy(false);
      if (diff) setDiff(null);
      else if (pages.current.length) { const previous = pages.current.pop()!; setPage(previous.page); setSelected(previous.selected); }
      else onDismiss();
      return;
    }
    const delta = matchChord("up", text, key) ? -1 : matchChord("down", text, key) ? 1 : 0;
    if (diff) { if (delta) scroll.current?.scrollBy(delta); }
    else if (delta) setSelected(value => Math.max(0, Math.min((page?.commits.length ?? 1) - 1, value + delta)));
    if (matchChord("enter", text, key) && !busy && !diff) void readDiff();
  });
  const visible = Math.max(1, Math.floor((h - 10) / 2));
  const start = Math.max(0, Math.min(selected - Math.floor(visible / 2), (page?.commits.length ?? 0) - visible));
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Box flexDirection="column" flexShrink={0}>
    <Text bold>{t({en: "Git parent history", zh: "Git 提交父关系"})} · {head}</Text>
    <Text>{t({en: "Arrows name actual parents; Publish stages and squash sources are shown in the item.", zh: "箭头指向真实父提交；业务阶段与 squash 来源映射在事项中单独显示。"})}</Text>
    {page ? <Text>{page.head_oid}</Text> : null}
    {busy ? <Text>{t({en: "Reading fixed Git objects…", zh: "正在读取固定 Git 对象…"})}</Text> : null}
    {error ? <Text>{safe(error)} · {t({en: "/refresh rereads this item", zh: "/refresh 重新读取当前事项"})}</Text> : null}
    </Box>
    {diff ? <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      <Text>{diff.base_oid || t({en: "Empty tree", zh: "空树"})} → {diff.head_oid}</Text>
      <Text>{safe(diff.text)}</Text>
      {diff.truncated ? <Text>{t({en: "Diff reached its display limit.", zh: "差异已达到显示上限。"})}</Text> : null}
    </ScrollBox> : <Box flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden">
      {page?.commits.slice(start, start + visible).map((commit, index) => <Box key={commit.oid} flexDirection="column">
        <Text bold={start + index === selected} wrap="truncate-end">{start + index === selected ? "› " : "  "}{commit.oid.slice(0, 12)} ← {commit.parents.map(oid => oid.slice(0, 12)).join(" + ") || t({en: "root", zh: "根提交"})}</Text>
        <Text wrap="truncate-end">  {safe(commit.subject)} · {safe(commit.author.name)}</Text>
      </Box>)}
      {page?.next_cursor ? <Text>{t({en: "/more continues this fixed history", zh: "/more 继续读取此固定历史"})}</Text> : null}
    </Box>}
  </Box>;
}
