import {SelectionRow, SelectionText} from "./Selection.js";
import React, {useEffect, useLayoutEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import type {PublishItem, PublishPlatformSnapshot} from "../backend/publish.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {publishGet} from "../backend/tools.js";
import {PublishPrDetails} from "./PublishPrDetails.js";
import {useManagedInput} from "../input/runtime.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {useFeedback} from "../hooks/useFeedback.js";

export function PublishRepositoryBrowser({client, project, items, localState, onFooter, onDismiss, onBind}: {
  client: BackendClient; project: string; items: PublishItem[]; localState: PublishLocalState;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
  onBind: (item: PublishItem, values: Record<string, string>) => void;
}) {
  const t = useCopy();
  const [target,setTarget]=useState<PublishPlatformSnapshot["target"]|null>(null),[configured,setConfigured]=useState(false);
  const help=useLocalHelpCommand("publish.collaboration",onFooter);
  const [snapshot, setSnapshot] = useState<PublishPlatformSnapshot | null>(null), [busy, setBusy] = useState(false);
  const feedback = useFeedback({kind: "page", id: `${project}:publish/repository`}, {tone: "error"});
  const error = feedback.text, setError = feedback.show;
  const [selected, setSelected] = useState(0), [detail, setDetail] = useState(false), active = useRef(true),generation=useRef(0),reading=useRef(false);
  const listing = useRef<PublishPlatformSnapshot | null>(null), listingSelection = useRef(0);
  const scroll = useRef<ScrollBoxHandle>(null), selectedRow = useRef<DOMElement>(null);
  useEffect(() => {
    active.current=true;const id=++generation.current;setBusy(true);
    void publishGet(client,project).then((value:any)=>{
      if(!active.current||id!==generation.current)return;
      const saved=value.platform_target;
      setConfigured(true);
      if(saved&&["kind","instance","repository","account"].every(key=>typeof saved[key]==="string"&&saved[key])){
        setTarget(saved);void read("collaborate",{target:saved,resource:{kind:"pulls"},page:1});
      }else setBusy(false);
    }).catch((e:any)=>{if(active.current&&id===generation.current){setConfigured(true);setBusy(false);setError(String(e?.message||e));}});
    return()=>{active.current=false;generation.current++;};
  },[client,project]);
  useEffect(() => {if (!detail && !help.active) {onFooter({hidden: true}); return () => onFooter(null);}}, [detail, onFooter,help.active]);
  const documents = snapshot?.document ? [snapshot.document] : snapshot?.documents ?? [];
  useLayoutEffect(() => {if (selectedRow.current) scroll.current?.scrollIntoView(selectedRow.current);}, [selected, snapshot]);
  const read = async (_operation: string, args: Record<string, any>) => {
    if (reading.current) return;
    reading.current=true;const id=++generation.current;
    setBusy(true); setError("");
    try {
      const value = await client.callTool("publish.platform_read", {project, target: args.target, resource: args.resource, page: args.page}) as PublishPlatformSnapshot & {read_only: boolean; schema_version: number};
      if (!active.current||id!==generation.current) return;
      if (value.read_only !== true || value.schema_version !== 1) throw Error("PUBLISH_RESPONSE_MISMATCH");
      if (value.document) {
        if (!detail) listingSelection.current = selected;
        if (listing.current && JSON.stringify(listing.current.target) !== JSON.stringify(value.target)) listing.current = null;
      } else {listing.current = value; listingSelection.current = 0;}
      setSnapshot(value); setSelected(0); setDetail(Boolean(value.document));
    } catch (e: any) {if (active.current&&id===generation.current) setError(String(e?.message || e));}
    finally {reading.current=false;if (active.current&&id===generation.current) setBusy(false);}
  };
  useManagedInput((text, key) => {
    if(help.handle(text,key))return;
    if (matchChord("escape", text, key)) onDismiss();
    else if (matchChord("up", text, key)) setSelected(value => Math.max(0, value - 1));
    else if (matchChord("down", text, key)) setSelected(value => Math.min(Math.max(0,documents.length+(target?1:0)-1), value + 1));
    else if (matchChord("enter", text, key)) {
      if(!target)return;
      const document = documents[selected];
      if (document) {void read("collaborate", {target: snapshot!.target, resource: {kind: "pull", number: document.number ?? Number(document.id)}, page: 1});}
      else {void read("collaborate",{target,resource:{kind:"pulls"},page:snapshot?.more?(snapshot.page??1)+1:1});}
    }
  }, {isActive: !detail});
  const document = documents[selected];
  if (detail && snapshot && document) return <PublishPrDetails item={items.find(item => item.candidate_oid === document.refs?.head_sha)} snapshot={snapshot} document={document} localState={localState} onFooter={onFooter}
    onDismiss={() => {
      setDetail(false); setError("");
      if (listing.current) {setSnapshot(listing.current); setSelected(listingSelection.current);}
      else {void read("collaborate",{target:snapshot.target,resource:{kind:"pulls"},page:1});}
    }} onCommand={(_command, values) => {
      if (_command === "/remote") {void read("collaborate",{target:snapshot.target,resource:{kind:"pull",number:document.number??Number(document.id)},page:1});return;}
      const item = items.find(item => item.candidate_oid === document.refs?.head_sha);
      if (!item) {setError(t({en: "Prepare the exact PR source in Workspace before handling it. Browsing never creates a candidate.", zh: "处理前请先在工作区准备这个 PR 对应的固定来源；浏览不会创建候选。"})); return;}
      onBind(item, {...snapshot.target, resource: "pull", number: String(document.number ?? document.id)});
    }} status={error}/>;
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{snapshot?.target.repository} · {t({en: "Repository PR observations", zh: "仓库 PR 观察"})}</Text>
    {busy?<Text dimColor>{t({en:"Reading…",zh:"正在读取…"})}</Text>:null}
    {!target&&configured?<Text dimColor>{t({en:"Set the repository account in Configuration first. This view reuses that saved target.",zh:"请先在配置的账户页保存仓库账户；此处直接使用已保存的目标。"})}</Text>:null}
    {error ? <Text>{error}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {documents.map((entry, index) => <SelectionRow selected={index === selected} key={entry.id} ref={index === selected ? selectedRow : undefined} >
        <SelectionText selected={index === selected}   wrap="truncate-end">#{entry.number ?? entry.id} {(entry.title ?? "").replace(/[\x00-\x1f\x7f-\x9f]/g, " ")}</SelectionText>
      </SelectionRow>)}
      {target?<SelectionRow selected={selected === documents.length} ref={selected === documents.length ? selectedRow : undefined} >
        <SelectionText selected={selected === documents.length}>{snapshot?.more?t({en:"Next page",zh:"下一页"}):t({en:"Refresh PRs",zh:"刷新 PR"})}</SelectionText>
      </SelectionRow>:null}
    </ScrollBox>

  </Box>;
}
