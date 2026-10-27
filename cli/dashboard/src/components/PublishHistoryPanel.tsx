import React, {useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useTextInput, applyTextOp} from "../hooks/useTextInput.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {layoutCommitGraph, drawCommitGraph} from "./publishCommitGraph.js";

type Commit = {oid: string; parents: string[]; subject: string; author: {name: string; email: string; timestamp: number}};
type History = {snapshot_id: string; head_oid: string; relation: string; commits: Commit[]; next_cursor: string | null};
type Difference = {text: string; truncated?: boolean; base_oid: string; head_oid: string};
type Position = {cursor: string; frontier: (string | null)[]; selected: string};
type Saved = Position & {stack: Position[]; diffParent?: number; top?: number};
const safe = (text: string) => text.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, " ");

export function PublishHistoryPanel({client, project, itemId, head, onFooter, onDismiss, localState}: {
  client: BackendClient; project: string; itemId: string; head: string; localState?: PublishLocalState;
  onFooter: (footer: FooterConfig | null) => void; onDismiss: () => void;
}) {
  const t = useCopy(), input = useTextInput(), {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const key = "history/" + itemId + "/" + head;
  const [saved] = useState(() => localState?.get<Saved>(key));
  const [page, setPage] = useState<History | null>(null), [selected, setSelected] = useState(0);
  const [frontier, setFrontier] = useState<(string | null)[]>(saved?.frontier ?? []);
  const [diff, setDiff] = useState<Difference | null>(null), [diffParent, setDiffParent] = useState<number | undefined>(undefined);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const generation = useRef(0), scroll = useRef<ScrollBoxHandle>(null);
  const position = useRef<Position>({cursor: saved?.cursor ?? "", frontier: saved?.frontier ?? [], selected: saved?.selected ?? ""});
  const pages = useRef<Position[]>(saved?.stack ?? []), restoring = useRef(true), top = useRef(saved?.top ?? 0);
  const graph = useMemo(() => layoutCommitGraph(page?.commits ?? [], frontier), [page, frontier]);
  const remember = () => {
    if (!page) return;
    position.current.selected = page.commits[selected]?.oid ?? "";
    localState?.set(key, {...position.current, stack: pages.current, diffParent, top: scroll.current?.getScrollTop() ?? top.current});
  };
  const read = async (next: Position, push = false) => {
    const current = ++generation.current; setBusy(true); setError("");
    try {
      const result = await client.callTool("publish.history", {project, item_id: itemId, head, limit: 50, cursor: next.cursor}) as History;
      if (current !== generation.current) return;
      if (result.relation !== "git_parent" || !Array.isArray(result.commits)) throw new Error("PUBLISH_RESPONSE_MISMATCH");
      if (push && page) { remember(); pages.current.push({...position.current}); }
      position.current = {...next}; setFrontier(next.frontier);
      setPage(result); setSelected(Math.max(0, result.commits.findIndex(row => row.oid === next.selected))); setDiff(null); setDiffParent(undefined);
    } catch (failure: any) {
      if (current !== generation.current) return;
      if (restoring.current && next.cursor) {
        pages.current = []; restoring.current = false; top.current = 0;
        await read({cursor: "", frontier: [], selected: next.selected});
      } else setError(String(failure?.code || failure?.message));
    } finally { if (current === generation.current) setBusy(false); }
  };
  const readDiff = async (parent = 0, restore = false) => {
    const commit = page?.commits[selected];
    if (!commit || parent < 0 || parent >= Math.max(1, commit.parents.length)) return;
    const current = ++generation.current; setBusy(true); setError("");
    try {
      const result = await client.callTool("publish.diff", {project, item_id: itemId, head, snapshot_id: page!.snapshot_id,
        base_oid: commit.parents[parent] ?? "", head_oid: commit.oid, limit_bytes: 262144}) as Difference;
      if (current !== generation.current) return;
      top.current = restore ? saved?.top ?? 0 : 0;
      setDiff(result); setDiffParent(parent);
    } catch (failure: any) { if (current === generation.current) setError(String(failure?.code || failure?.message)); }
    finally { if (current === generation.current) setBusy(false); }
  };
  useEffect(() => { void read(position.current); return () => { generation.current++; }; }, [project, itemId, head]);
  useEffect(() => {
    if (!page || !restoring.current) return;
    restoring.current = false;
    if (saved?.diffParent !== undefined && page.commits[selected]?.oid === saved.selected) void readDiff(saved.diffParent, true);
  }, [page]);
  useLayoutEffect(() => { remember(); }, [page, selected, diffParent]);
  useLayoutEffect(() => {
    const handle = scroll.current;
    if (!handle || !diff) return;
    handle.scrollTo(top.current);
    const timer = setTimeout(() => {
      top.current = handle.getScrollTop();
      localState?.set(key, {...position.current, stack: pages.current, diffParent, top: top.current});
    }, 25);
    const unsubscribe = handle.subscribe(() => {
      top.current = Math.max(0, handle.getScrollTop() + handle.getPendingDelta());
      localState?.set(key, {...position.current, stack: pages.current, diffParent, top: top.current});
    });
    return () => { clearTimeout(timer); top.current = handle.getScrollTop(); unsubscribe(); };
  }, [diff]);
  useEffect(() => {
    onFooter({kind: "command", cmdInput: input, suggestions: [], suggestionIdx: 0, cmdResult: "",
      statusText: t({en: "←/→ commit · Enter diff · /more · /parent N · Esc back", zh: "←/→ 选择提交 · Enter 差异 · /more 下一页 · /parent N 父提交 · Esc 返回"})});
    return () => onFooter(null);
  }, [input, input.value, input.cursor, onFooter, t]);
  useInput((text, key) => {
    if (input.value.startsWith("/")) {
      if (matchChord("escape", text, key)) input.setValue("");
      else if (matchChord("enter", text, key)) {
        const command = input.value.trim(); input.setValue("");
        if (busy) return;
        if (command === "/more" && page?.next_cursor) void read({cursor: page.next_cursor, frontier: graph.frontier, selected: ""}, true);
        else if (command === "/refresh") { pages.current = []; void read({cursor: "", frontier: [], selected: page?.commits[selected]?.oid ?? ""}); }
        else if (/^\/parent [1-9]\d*$/.test(command)) void readDiff(Number(command.split(" ")[1]) - 1);
      } else if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
      else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
      return;
    }
    if (text === "/") { input.setValue("/"); return; }
    if (matchChord("escape", text, key)) {
      generation.current++; setBusy(false);
      if (diff) { setDiff(null); setDiffParent(undefined); }
      else if (pages.current.length) void read(pages.current.pop()!);
      else { remember(); onDismiss(); }
      return;
    }
    const delta = matchChord("up", text, key) || matchChord("left", text, key) ? -1 : matchChord("down", text, key) || matchChord("right", text, key) ? 1 : 0;
    if (diff) { if (delta) scroll.current?.scrollBy(delta); }
    else if (delta) setSelected(value => Math.max(0, Math.min((page?.commits.length ?? 1) - 1, value + delta)));
    if (matchChord("enter", text, key) && !busy && !diff) void readDiff();
  });
  const commit = page?.commits[selected];
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Box flexDirection="column" flexShrink={0}>
      <Text bold>{t({en: "Git branch / merge graph", zh: "Git 分支 / 合并图"})} · {head}</Text>
      <Text wrap="truncate-end">{t({en: "Newer → older · O selected · * commit · > continues · actual Git parents", zh: "左新右旧 · O 选中 · * 提交 · > 延续 · 连线为真实父关系"})}</Text>
      {page ? <Text wrap="truncate-end">{page.head_oid}</Text> : null}
      {busy ? <Text>{t({en: "Reading fixed Git objects…", zh: "正在读取固定 Git 对象…"})}</Text> : null}
      {error ? <Text>{safe(error)} · /refresh</Text> : null}
    </Box>
    {diff ? <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      <Text>{diff.base_oid || t({en: "Empty tree", zh: "空树"})} → {diff.head_oid}</Text>
      <Text>{safe(diff.text)}</Text>
      {diff.truncated ? <Text>{t({en: "Diff reached its display limit.", zh: "差异已达到显示上限。"})}</Text> : null}
    </ScrollBox> : <Box flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden">
      {drawCommitGraph(graph, selected, w, Math.max(3, h - 11)).map((line, index) => <Text key={index} wrap="truncate-end">{line}</Text>)}
      {commit ? <>
        <Text bold wrap="truncate-end">{selected + 1}/{page!.commits.length} · {commit.oid.slice(0, 12)} · {safe(commit.subject)}</Text>
        <Text wrap="truncate-end">{safe(commit.author.name)} · {t({en: "Parents", zh: "父提交"})}: {commit.parents.map(oid => oid.slice(0, 12)).join(" + ") || t({en: "root", zh: "根提交"})}</Text>
      </> : null}
      {page?.next_cursor ? <Text>/more →</Text> : null}
    </Box>}
  </Box>;
}
