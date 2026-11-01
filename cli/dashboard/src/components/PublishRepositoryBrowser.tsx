import {SelectionRow, SelectionText} from "./Selection.js";
import React, {useEffect, useLayoutEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import type {PublishItem, PublishPlatformSnapshot} from "../backend/publish.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {PublishOperationDraft} from "./PublishOperationDraft.js";
import {PublishPrDetails} from "./PublishPrDetails.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {} from "../theme/index.js";
import {useFeedback} from "../hooks/useFeedback.js";

export function PublishRepositoryBrowser({client, project, items, localState, onFooter, onDismiss, onBind}: {
  client: BackendClient; project: string; items: PublishItem[]; localState: PublishLocalState;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
  onBind: (item: PublishItem, values: Record<string, string>) => void;
}) {
  const t = useCopy();
  const [form, setForm] = useState(true), [initial, setInitial] = useState<Record<string, string>>({resource: "pulls"});
  const [snapshot, setSnapshot] = useState<PublishPlatformSnapshot | null>(null), [busy, setBusy] = useState(false);
  const feedback = useFeedback({kind: "page", id: `${project}:publish/repository`}, {tone: "error"});
  const error = feedback.text, setError = feedback.show;
  const [selected, setSelected] = useState(0), [detail, setDetail] = useState(false), active = useRef(true);
  const listing = useRef<PublishPlatformSnapshot | null>(null), listingSelection = useRef(0);
  const scroll = useRef<ScrollBoxHandle>(null), selectedRow = useRef<DOMElement>(null);
  useEffect(() => {active.current = true; return () => {active.current = false;};}, []);
  useEffect(() => {if (!form && !detail) {onFooter({hidden: true}); return () => onFooter(null);}}, [form, detail, onFooter]);
  const documents = snapshot?.document ? [snapshot.document] : snapshot?.documents ?? [];
  useLayoutEffect(() => {if (selectedRow.current) scroll.current?.scrollIntoView(selectedRow.current);}, [selected, snapshot]);
  const read = async (_operation: string, args: Record<string, any>) => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const value = await client.callTool("publish.platform_read", {project, target: args.target, resource: args.resource, page: args.page}) as PublishPlatformSnapshot & {read_only: boolean; schema_version: number};
      if (!active.current) return;
      if (value.read_only !== true || value.schema_version !== 1) throw Error("PUBLISH_RESPONSE_MISMATCH");
      if (value.document) {
        if (!detail) listingSelection.current = selected;
        if (listing.current && JSON.stringify(listing.current.target) !== JSON.stringify(value.target)) listing.current = null;
      } else {listing.current = value; listingSelection.current = 0;}
      setSnapshot(value); setForm(false); setSelected(0); setDetail(Boolean(value.document));
    } catch (e: any) {if (active.current) setError(String(e?.message || e));}
    finally {if (active.current) setBusy(false);}
  };
  useManagedInput((text, key) => {
    if (matchChord("escape", text, key)) onDismiss();
    else if (matchChord("up", text, key)) setSelected(value => Math.max(0, value - 1));
    else if (matchChord("down", text, key)) setSelected(value => Math.min(documents.length, value + 1));
    else if (matchChord("enter", text, key)) {
      const document = documents[selected];
      if (document) {void read("collaborate", {target: snapshot!.target, resource: {kind: "pull", number: document.number ?? Number(document.id)}, page: 1});}
      else {setInitial({...snapshot?.target, resource: "pulls", page: String((snapshot?.page ?? 0) + 1)}); setForm(true);}
    }
  }, {isActive: !form && !detail});
  if (form) return <PublishOperationDraft mode="remote" busy={busy} status={error} initialValues={initial} localState={localState}
    formKey={`repository/read/${initial.resource}/${initial.number ?? "list"}`} onFooter={onFooter} onDismiss={() => snapshot ? setForm(false) : onDismiss()} onSubmit={(op, args) => void read(op, args)}/>;
  const document = documents[selected];
  if (detail && snapshot && document) return <PublishPrDetails item={items.find(item => item.candidate_oid === document.refs?.head_sha)} snapshot={snapshot} document={document} localState={localState} onFooter={onFooter}
    onDismiss={() => {
      setDetail(false); setError("");
      if (listing.current) {setSnapshot(listing.current); setSelected(listingSelection.current);}
      else {setInitial({...snapshot.target, resource: "pulls", page: "1"}); setForm(true);}
    }} onCommand={(_command, values) => {
      if (_command === "/remote") {setInitial({...snapshot.target, resource: "pull", number: String(document.number ?? document.id)}); setForm(true); return;}
      const item = items.find(item => item.candidate_oid === document.refs?.head_sha);
      if (!item) {setError(t({en: "Prepare the exact PR source in Workspace before handling it. Browsing never creates a candidate.", zh: "处理前请先在 Workspace 准备这个 PR 对应的固定来源；浏览不会创建候选。"})); return;}
      onBind(item, {...snapshot.target, resource: "pull", number: String(document.number ?? document.id)});
    }} status={error}/>;
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{snapshot?.target.repository} · {t({en: "Repository PR observations", zh: "仓库 PR 观察"})}</Text>
    {error ? <Text>{error}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {documents.map((entry, index) => <SelectionRow selected={index === selected} key={entry.id} ref={index === selected ? selectedRow : undefined} >
        <SelectionText selected={index === selected}   wrap="truncate-end">#{entry.number ?? entry.id} {(entry.title ?? "").replace(/[\x00-\x1f\x7f-\x9f]/g, " ")}</SelectionText>
      </SelectionRow>)}
      <SelectionRow selected={selected === documents.length} ref={selected === documents.length ? selectedRow : undefined} >
        <SelectionText selected={selected === documents.length}  >{t({en: "Read / refresh another page", zh: "读取或刷新其他页"})}</SelectionText>
      </SelectionRow>
    </ScrollBox>
    <Text dimColor>{t({en: "↑/↓ select · Enter read exact PR · Esc back", zh: "↑/↓ 选择 · Enter 读取具体 PR · Esc 返回"})}</Text>
  </Box>;
}
