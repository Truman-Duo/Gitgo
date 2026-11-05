import React, {useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, TerminalRaster, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import {publishItems, type PublishItem, type PublishSnapshot, type PlatformRecord, type PublishPlatformSnapshot} from "../backend/publish.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {colors, separator, truncate, usePanelSize} from "../theme/index.js";
import {SelectionRow, SelectionText} from "./Selection.js";
import {HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";
import {PublishItemPanel} from "./PublishItemPanel.js";
import {PublishPanel} from "./PublishPanel.js";
import {PublishHistoryPanel} from "./PublishHistoryPanel.js";
import {createPublishStateStore} from "../state/publishLocalState.js";
import {layoutCommitGraph, drawCommitGraph} from "./publishCommitGraph.js";
import {workspaceGraphSvg, commitGraphSvg} from "./publishGraphSvg.js";
import {useGraphicsCapability} from "../ui/terminalGraphics.js";
import {useSvgRaster} from "../ui/svgRaster.js";
import {PublishPrDetails} from "./PublishPrDetails.js";
import {PublishRepositoryBrowser} from "./PublishRepositoryBrowser.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {publishSuggestions} from "./publishCommands.js";

type Props = React.ComponentProps<typeof PublishPanel>;
type Commit = {oid: string; parents: string[]; subject: string};
type Formal = Commit & {item_id: string; operation_id: string; sources: string[]; target_ref: string};
type Workspace = {schema_version: number; snapshot_id: string; head_oid: string; commits: Commit[];
  formal_commits: Formal[]; formal_total: number; next_cursor: string | null};
type Row = {id: string; label: string; detail?: string; run: () => void};
type Operation = {itemId?: string; command?: string; values?: Record<string, string>};
const safe = (value: string) => value.replace(/[\x00-\x1f\x7f-\x9f]/g, " ");

/** Project-scoped terminal navigation. All effects stay in the existing operation controller. */
export function PublishWorkbench(props: Props) {
  const {client, project, onFooter, onDismiss} = props, t = useCopy();
  const {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [local] = useState(() => props.localState ?? createPublishStateStore().scope(project));
  const [saved] = useState(() => local.get<{tab: number; sub: number; source: string; formal: string; marked: string[]; rowId?: string}>("workbench.view"));
  const [tab, setTab] = useState(Math.max(0, Math.min(3, saved?.tab ?? 0))), [sub, setSub] = useState(Math.max(0, Math.min(1, saved?.sub ?? 0)));
  const [focus, setFocus] = useState<"header" | "sub" | "body">("header"), [row, setRow] = useState(0);
  const [snapshot, setSnapshot] = useState<PublishSnapshot | null>(null), [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [source, setSource] = useState(saved?.source ?? ""), [formal, setFormal] = useState(saved?.formal ?? "");
  const [marked, setMarked] = useState<string[]>(saved?.marked ?? []);
  const [outgoing, setOutgoing] = useState<string[]>([]);
  const [operation, setOperation] = useState<Operation | null>(null), [history, setHistory] = useState<{itemId: string; head: string} | null>(null);
  const [repositoryBrowser, setRepositoryBrowser] = useState(false);
  const [pr, setPr] = useState<{item: PublishItem; snapshot: PublishPlatformSnapshot; document: PlatformRecord} | null>(null);
  const [loading, setLoading] = useState(false);
  const view = `${project}:${tab}:${sub}:${operation ? "operation" : history ? "history" : repositoryBrowser ? "repository" : pr ? "pr:" + pr.document.id : "root"}`;
  const pageFeedback = useFeedback({kind: "page", id: view}, {tone: "error"});
  // A confirmed operation result may follow the user across this project's tabs.
  // It is still transient; durable uncertainty remains in the saved item itself.
  const operationFeedback = useFeedback({kind: "project", id: project});
  const setError = pageFeedback.show, setNotice = operationFeedback.show;
  const [pageStack, setPageStack] = useState<string[]>([]), cursor = useRef("");
  const generation = useRef(0), selectedElement = useRef<DOMElement>(null), scroll = useRef<ScrollBoxHandle>(null), selectedId = useRef(saved?.rowId ?? "");
  const items = snapshot?.items ?? [], commits = workspace?.commits ?? [], formalCommits = workspace?.formal_commits ?? [];
  const sourceIndex = Math.max(0, commits.findIndex(c => c.oid === source)), formalIndex = Math.max(0, formalCommits.findIndex(c => c.oid === formal));
  const sourceCommit = commits[sourceIndex], formalCommit = formalCommits[formalIndex];
  const open = (command: string, item?: PublishItem, values?: Record<string, string>) => setOperation({command, itemId: item?.id, values});
  const read = useCallback(async (pageCursor = "", moreItems = false) => {
    const id = ++generation.current; setLoading(true); setError("");
    const results = await Promise.allSettled([
      publishItems(client, project, moreItems ? snapshot?.next_cursor ?? "" : ""),
      client.callTool("publish.workspace", {project, limit: 50, cursor: pageCursor}) as Promise<Workspace>,
    ]);
    if (generation.current !== id) return;
    const [records, tree] = results;
    if (records.status === "fulfilled") {
      if (moreItems && snapshot && snapshot.snapshot_id !== records.value.snapshot_id) setError("PUBLISH_SNAPSHOT_CHANGED");
      else setSnapshot(moreItems && snapshot ? {...records.value, items: [...snapshot.items, ...records.value.items.filter(n => !snapshot.items.some(p => p.id === n.id))]} : records.value);
    } else setError(String(records.reason?.message || records.reason));
    if (tree.status === "fulfilled") {
      if (tree.value.schema_version !== 1 || !Array.isArray(tree.value.commits) || !Array.isArray(tree.value.formal_commits)) setError("PUBLISH_RESPONSE_MISMATCH");
      else { setWorkspace(tree.value); cursor.current = pageCursor; }
    } else setError(String(tree.reason?.message || tree.reason));
    setLoading(false);
  }, [client, project, snapshot, setError]);
  useEffect(() => { void read(); return () => { generation.current++; }; }, [client, project]);
  const returned = (item?: PublishItem, effectState?: string) => {
    if (item && pr) {
      const fresh = item.collaboration?.snapshots.findLast(s => s.resource.kind === "pull" && String(s.resource.number) === String(pr.document.number ?? pr.document.id));
      if (fresh?.document) setPr({item, snapshot: fresh, document: fresh.document});
    }
    if (item) { setFormal(item.operations?.findLast(o => o.state === "observed" && o.result_oid)?.result_oid ?? ""); selectedId.current = item.id; }
    setOperation(null); setNotice(t(effectState === "ambiguous" ? {en: "The result is uncertain. Read the saved item and reconcile before repeating an action.", zh: "结果尚未确认，请读取已保存事项并核对，再决定下一步。"}
      : {en: "Saved result refreshed from the project.", zh: "已保存，正在刷新项目中的实际结果。"}), {tone: effectState === "ambiguous" ? "warning" : "success"}); void read();
  };
  const prepare = () => {
    if (marked.some(oid => !commits.some(c => c.oid === oid))) {setError(t({en: "Review or clear marked commits outside this page first.", zh: "请先核对或清空位于其他页的选择。"}), {tone: "warning"}); return;}
    const selected = commits.filter(c => marked.length ? marked.includes(c.oid) : c.oid === sourceCommit?.oid);
    if (!selected.length || selected.length > 32) { setError("PUBLISH_INVALID_SELECTION"); return; }
    const oldest = selected.at(-1)!;
    if (!oldest.parents.length) { setError(t({en: "A source range needs a parent baseline. Select commits after the repository root.", zh: "整理范围需要父提交作为基线，请选择仓库根提交之后的记录。"}), {tone: "warning"}); return; }
    const previous = formalCommits.find(commit => commit.target_ref !== "refs/heads/publish/outgoing");
    open("/formal", undefined, {source_role: "workspace", source_ref: selected[0]!.oid,
      selected_commits: selected.map(c => c.oid).join(" "), target_role: "workspace",
      target_branch: previous?.target_ref.replace(/^refs\/heads\//, "") ?? "publish/formal",
      ...(!previous ? {base_oid: oldest.parents[0]!} : {}), title: "", method: "squash"});
  };
  const incoming = items.flatMap(item => [...(item.collaboration?.snapshots ?? [])].reverse().flatMap(s => {
    if (!["pull", "pulls"].includes(s.resource.kind)) return [];
    const docs = s.document ? [s.document] : s.documents ?? [];
    return docs.map(document => ({item, snapshot: s, document}));
  })).filter((value, i, all) => all.findIndex(other => other.snapshot.target.instance === value.snapshot.target.instance && other.snapshot.target.repository === value.snapshot.target.repository && other.document.id === value.document.id) === i);
  const contributions = items.filter(item => item.identity_kind === "stable_item" && !item.recorded?.is_incoming);
  const semantic = formalCommits.filter(commit => commit.target_ref !== "refs/heads/publish/outgoing");
  const selectedItem = items.find(item => item.id === (tab === 1 && sub === 0 ? semantic[row]?.item_id : selectedId.current)) ?? items.find(item => item.id === selectedId.current) ?? contributions[0] ?? items[0];
  const rows: Row[] = [];
  if (tab === 0) {
    rows.push({id: "process", label: t({en: "Process Git", zh: "过程 Git"}), detail: sourceCommit ? `${sourceCommit.oid.slice(0, 8)}  ${sourceCommit.subject}` : t({en: "No local Git history available", zh: "尚无可读取的本地 Git 历史"}), run: () => setHistory({itemId: "", head: "workspace"})},
      {id: "prepare", label: t({en: `Consolidate ${marked.length || (sourceCommit ? 1 : 0)} selected records`, zh: `整理所选 ${marked.length || (sourceCommit ? 1 : 0)} 条记录`}),  run: prepare},
      {id: "formal", label: t({en: "Local formal Git", zh: "本地正式 Git"}), detail: formalCommit ? `${formalCommit.oid.slice(0, 8)}  ${formalCommit.subject}` : t({en: "No observed local formal commit yet", zh: "尚无已核对的本地正式提交"}), run: () => {if (formalCommit) setHistory({itemId: formalCommit.item_id, head: `formal_operation:${formalCommit.operation_id}`});}});
    for (const item of items.filter(i => i.plan?.destination === "formal" && i.status !== "integrated")) rows.push({id: item.id, label: item.title, detail: `${item.status} · ${item.plan?.target_role ?? "formal"} · ${item.target}`, run: () => setOperation({itemId: item.id})});
    rows.push({id: "collaborate", label: t({en: "Continue in collaboration", zh: "进入协作"}),  run: () => {selectedId.current = formalCommit?.item_id ?? ""; setTab(1); setSub(0); setRow(0);}});
    if (workspace?.next_cursor) rows.push({id: "more-history", label: t({en: "Earlier process history", zh: "更早的过程记录"}), run: () => {setPageStack(v => [...v, cursor.current]); void read(workspace.next_cursor!);}});
    if (pageStack.length) rows.push({id: "previous-history", label: t({en: "Return to newer records", zh: "返回较新的记录"}), run: () => {const next = [...pageStack]; void read(next.pop() ?? ""); setPageStack(next);}});
    if (marked.length) rows.push({id: "clear-selection", label: t({en: "Clear source selection", zh: "清空来源选择"}), run: () => setMarked([])});
  } else if (tab === 1 && sub === 0) {
    for (const commit of semantic) rows.push({id: commit.item_id, label: `${outgoing.includes(commit.oid) ? "✓ " : ""}${commit.subject}`, detail: `${commit.oid.slice(0, 8)} · ${commit.target_ref}`, run: () => {selectedId.current = commit.item_id; setOperation({itemId: commit.item_id});}});
    if (selectedItem) rows.push(...[
      {id: "send-branch", label: t({en: "Publish the formal branch as a series", zh: "保留正式提交序列，接到远端分支"}),  run: () => {const latest = semantic[0]; if (!latest) {setError("PUBLISH_ACTION_UNAVAILABLE"); return;} setOperation({command: "/branch", itemId: latest.item_id, values: {action: "prepare_publish", branch: latest.target_ref.replace(/^refs\/heads\//, "")}});}},
      {id: "squash-outgoing", label: t({en: "Squash selected formal commits before publication", zh: "合并所选正式提交，再准备发布"}),  run: () => {
        const chosen = semantic.filter(commit => outgoing.length ? outgoing.includes(commit.oid) : commit.oid === semantic[0]?.oid);
        if (!chosen.length || chosen.length > 32 || !chosen.at(-1)?.parents.length) {setError("PUBLISH_INVALID_SELECTION"); return;}
        open("/formal", undefined, {target_role: "workspace", source_role: "workspace", source_ref: chosen[0]!.oid, base_oid: chosen.at(-1)!.parents[0]!,
          target_branch: "publish/outgoing", selected_commits: chosen.map(commit => commit.oid).join(" "), title: "", method: "squash"});
      }},
      {id: "outgoing-pr", label: t({en: "Prepare or update my PR", zh: "准备或更新我的 PR"}), detail: t({en: "Save a platform draft; sending is a separate explicit action.", zh: "先保存平台草稿，发送为另一个明确操作。"}), run: () => open("/remote", selectedItem, {resource: "pulls"})},
    ]);
  } else if (tab === 1) {
    for (const entry of incoming) rows.push({id: `${entry.item.id}/${entry.document.id}`, label: `#${entry.document.number ?? entry.document.id} ${entry.document.title ?? ""}`, detail: `${entry.document.state ?? ""} · ${entry.snapshot.target.repository}`, run: () => {setPr(entry); setRow(0);}});
    rows.push({id: "refresh-pr", label: t({en: "Read repository PRs", zh: "读取仓库 PR"}), detail: t({en: "Read platform facts even before preparing a local candidate.", zh: "尚无本地候选时也可以读取平台事实。"}), run: () => setRepositoryBrowser(true)});
  } else if (tab === 2) {
    for (const item of items.filter(i => i.status === "integrated")) {
      if (sub === 0) rows.push({id: item.id, label: item.title, detail: `${item.target} · ${item.packages?.length ?? 0} ${t({en: "artifacts", zh: "项产物"})}`, run: () => setOperation({itemId: item.id})});
      else for (const pkg of item.packages ?? []) rows.push({id: pkg.id, label: `${pkg.version} · ${pkg.filename}`, detail: `${pkg.source_oid.slice(0, 8)} · ${t({en: "Artifact record; inspect tag and release receipts separately", zh: "产物记录；标签与平台发布状态请分别核对"})}`, run: () => setOperation({itemId: item.id})});
    }
  }
  if (!pr && tab !== 3 && snapshot?.next_cursor) rows.push({id: "more-items", label: t({en: "More saved items", zh: "更多已保存事项"}), run: () => void read(cursor.current, true)});
  const currentRow = rows[Math.min(row, Math.max(0, rows.length - 1))];
  useLayoutEffect(() => {setRow(value => Math.max(0, Math.min(value, rows.length - 1)));}, [tab, sub, pr, rows.length]);
  useLayoutEffect(() => {if (selectedElement.current) scroll.current?.scrollIntoView(selectedElement.current);}, [row, tab, sub, w, h]);
  useEffect(() => {local.set("workbench.view", {tab, sub, source: sourceCommit?.oid, formal: formalCommit?.oid, marked, rowId: currentRow?.id});}, [tab, sub, sourceCommit?.oid, formalCommit?.oid, marked, currentRow?.id]);
  const child = operation || history || pr || repositoryBrowser || tab === 3 && focus === "body";
  const helpTopic=tab===0?"publish.workspace":tab===1?"publish.collaboration":tab===2?"publish.release":"publish.settings";
  const help=useLocalHelpCommand(helpTopic,onFooter,!child,focus==="body"?{suggest:input=>publishSuggestions({item:selectedItem,more:Boolean(snapshot?.next_cursor)},input,t),run:command=>setOperation({itemId:selectedItem?.id,command})}:undefined);
  const active = items.some(item => ["queued", "running", "stopping"].includes(item.run?.state ?? "") || ["checking", "pausing", "executing"].includes(item.status));
  useEffect(() => {
    if (child || !active) return;
    const timer = setInterval(() => {void read(cursor.current);}, 1000);
    return () => clearInterval(timer);
  }, [Boolean(child), active, read]);
  useEffect(() => {if (!child&&!help.active) {onFooter({hidden: true}); return () => onFooter(null);}}, [Boolean(child), onFooter,help.active]);
  const secondary = tab === 1 ? [t({en: "My contributions", zh: "我的提交"}), t({en: "Incoming PRs", zh: "收到的 PR"})]
    : tab === 2 ? [t({en: "Release preparation", zh: "发布准备"}), t({en: "Existing versions", zh: "已有版本"})] : [];
  useManagedInput((text, key) => {
    if(help.handle(text,key))return;
    if (matchChord("escape", text, key)) {
      if (pr) {setPr(null); setRow(0);}
      else if (focus === "header") onDismiss();
      else setFocus(focus === "body" && secondary.length ? "sub" : "header");
      return;
    }
    if (focus === "header") {
      const delta = matchChord("left", text, key) ? -1 : matchChord("right", text, key) ? 1 : 0;
      if (delta) {setTab(v => Math.max(0, Math.min(3, v + delta))); setSub(0); setRow(0); setPr(null);}
      else if (matchChord("enter", text, key)) setFocus(secondary.length ? "sub" : "body");
      return;
    }
    if (focus === "sub") {
      const delta = matchChord("left", text, key) ? -1 : matchChord("right", text, key) ? 1 : 0;
      if (delta) {setSub(v => Math.max(0, Math.min(secondary.length - 1, v + delta))); setRow(0);}
      else if (matchChord("enter", text, key)) setFocus("body");
      else if (matchChord("up", text, key)) setFocus("header");
      return;
    }
    const delta = matchChord("left", text, key) ? -1 : matchChord("right", text, key) ? 1 : 0;
    if (tab === 0 && delta && currentRow?.id === "process") setSource(commits[Math.max(0, Math.min(commits.length - 1, sourceIndex + delta))]?.oid ?? "");
    else if (tab === 0 && delta && currentRow?.id === "formal") setFormal(formalCommits[Math.max(0, Math.min(formalCommits.length - 1, formalIndex + delta))]?.oid ?? "");
    else if (matchChord("up", text, key)) setRow(v => Math.max(0, v - 1));
    else if (matchChord("down", text, key)) setRow(v => Math.max(0, Math.min(rows.length - 1, v + 1)));
    else if (text === " " && tab === 0 && currentRow?.id === "process" && sourceCommit) setMarked(v => v.includes(sourceCommit.oid) ? v.filter(oid => oid !== sourceCommit.oid) : [...v, sourceCommit.oid]);
    else if (text === " " && tab === 1 && sub === 0) {const commit = semantic.find(commit => commit.item_id === currentRow?.id); if (commit) setOutgoing(value => value.includes(commit.oid) ? value.filter(oid => oid !== commit.oid) : [...value, commit.oid]);}
    else if (matchChord("enter", text, key)) currentRow?.run();
    else if (key.ctrl && text === "r") {setPageStack([]); void read();}
  }, {isActive: !child});
  const processGraph = useMemo(() => layoutCommitGraph(commits, [], workspace?.head_oid), [workspace]);
  const formalGraph = useMemo(() => layoutCommitGraph(formalCommits, [], formalCommits[0]?.oid), [workspace]);
  const collaborationGraph = useMemo(() => layoutCommitGraph(semantic, [], semantic[0]?.oid), [workspace]);
  const capability = useGraphicsCapability(), graphRows = Math.max(4, Math.min(9, h - 14));
  const scene = useMemo(() => !child && capability.sixel ? tab === 0 ? workspaceGraphSvg(processGraph, formalGraph, formalCommits,
    sourceIndex, formalIndex, currentRow?.id === "formal" ? "formal" : "process", w, graphRows, capability)
    : tab === 1 ? commitGraphSvg(collaborationGraph, Math.min(row, Math.max(0, semantic.length - 1)), w, graphRows, capability) : null : null,
    [tab, Boolean(child), capability, workspace, sourceIndex, formalIndex, row, currentRow?.id, w, graphRows]);
  const raster = useSvgRaster(scene);
  const header = <><HorizontalHeaderStrip items={["Workspace", t({en: "Collaboration", zh: "协作"}), t({en: "Releases", zh: "版本发布"}), t({en: "Configuration", zh: "配置"})].map((label, index) => ({id: String(index), label}))} selected={tab} width={w} contentFocused={focus !== "header" || Boolean(child)}/><Text color={colors.divider.color}>{separator(w)}</Text></>;
  if (operation) return <Box flexDirection="column" flexGrow={1} minHeight={0}>{header}<PublishItemPanel {...props} localState={local} initial={operation} onResult={returned} onDismiss={() => {setOperation(null); void read();}}/></Box>;
  if (history) return <Box flexDirection="column" flexGrow={1} minHeight={0}>{header}<PublishHistoryPanel {...props} itemId={history.itemId} head={history.head} localState={local} onDismiss={() => setHistory(null)}/></Box>;
  if (repositoryBrowser) return <Box flexDirection="column" flexGrow={1} minHeight={0}>{header}<PublishRepositoryBrowser client={client} project={project} items={items} localState={local} onFooter={onFooter} onDismiss={() => setRepositoryBrowser(false)}
    onBind={(item, values) => {setRepositoryBrowser(false); open("/remote", item, values);}}/></Box>;
  if (pr) return <Box flexDirection="column" flexGrow={1} minHeight={0}>{header}<FeedbackMessage feedback={pageFeedback.message}/><PublishPrDetails {...pr} onFooter={onFooter} localState={local} onDismiss={() => {setPr(null); setError("");}}
    onCommand={(command, values) => {
      if (["/work", "/flow", "/decide"].includes(command) && (!pr.document.refs?.head_sha || pr.item.candidate_oid !== pr.document.refs.head_sha)) {
        setError(t({en: "This PR source is not the item's fixed candidate. Bind and prepare the exact source before taking over or integrating it.", zh: "这个 PR 的来源尚未绑定到当前固定候选。接手或合入前需要先准备对应来源，避免处理错误的提交。"})); return;
      }
      open(command, pr.item, values);
    }}/></Box>;
  if (tab === 3 && focus === "body") return <Box flexDirection="column" flexGrow={1} minHeight={0}>{header}<PublishPanel {...props} localState={local} onOpenAccount={() => open("/account")} onDismiss={() => setFocus("header")}/></Box>;
  return <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
    {header}
    {secondary.length ? <HorizontalHeaderStrip items={secondary.map((label, index) => ({id: String(index), label}))} selected={sub} width={w} contentFocused={focus !== "sub"}/> : null}
    <Text dimColor wrap="truncate-end">{safe(project)}{loading ? t({en: " · Reading…", zh: " · 正在读取…"}) : ""}</Text>
    <FeedbackMessage feedback={pageFeedback.message}/>
    <FeedbackMessage feedback={operationFeedback.message}/>
    {local.error ? <Text color={colors.danger}>{safe(local.error)}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {tab === 0 && !pr ? <Box flexDirection="column">
        <Text dimColor>{t({en: "Process Git · local development history", zh: "过程 Git · 本地开发记录"})}</Text>
        {raster.image && capability.sixel ? <TerminalRaster image={raster.image} columns={w} rows={graphRows}/> : <>
          {drawCommitGraph(processGraph, sourceIndex, w, 3).map((line, i) => <Text key={`p${i}`}>{line}</Text>)}

          {drawCommitGraph(formalGraph, formalIndex, w, 3).map((line, i) => <Text key={`f${i}`}>{line}</Text>)}
        </>}
        <Text dimColor>{t({en: "Local formal Git · observed commits only", zh: "本地正式 Git · 仅展示已核对的提交"})} · {workspace?.formal_total ?? 0}</Text>
        {formalCommit ? <Text dimColor>{formalCommit.oid.slice(0, 8)} ← {formalCommit.sources.map(oid => oid.slice(0, 8)).join(" · ")}</Text> : null}
        {raster.error ? <Text dimColor>{t({en: "Vector graph unavailable; using the same rail layout in text.", zh: "矢量图不可用，当前显示同一线轨布局的文字兼容视图。"})}</Text> : null}
        {marked.some(oid => !commits.some(c => c.oid === oid)) ? <Text>{t({en: "Some marked commits are outside this page. Return to that page or clear selection before consolidation.", zh: "部分选择位于其他页，请返回核对或清空选择后整理。"})}</Text> : null}
      </Box> : null}

      {tab === 1 && semantic.length ? <Box flexDirection="column">
        <Text dimColor>{t({en: "Local formal Git · remote refs appear only after explicit observation", zh: "本地正式 Git · 远端引用需明确读取后核对"})}</Text>
        {raster.image && capability.sixel ? <TerminalRaster image={raster.image} columns={w} rows={graphRows}/> : drawCommitGraph(collaborationGraph, Math.min(row, semantic.length - 1), w, 3).map((line, index) => <Text key={index}>{line}</Text>)}
      </Box> : null}


      {rows.map((entry, index) => {
        const active = index === row && focus === "body";
        return <Box ref={index === row ? selectedElement : undefined} key={entry.id} flexDirection="column" marginTop={1}>
          <SelectionRow selected={active}><SelectionText wrap="truncate-end">{safe(entry.label)}</SelectionText></SelectionRow>
          {entry.detail ? <Text dimColor wrap="truncate-end">  {truncate(safe(entry.detail), Math.max(1, w - 2))}</Text> : null}
        </Box>;
      })}
      {!rows.length && tab !== 3 && !loading ? <Text dimColor>{t({en: "No saved records in this view.", zh: "此处尚无已保存记录。"})}</Text> : null}
    </ScrollBox>

  </Box>;
}
