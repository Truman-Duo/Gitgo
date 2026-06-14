// src/components/Detail.tsx
import React, { useState, useEffect, memo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import { McpClient } from "../mcp/client.js";

type Props = { projectName: string; client: McpClient; onBack: () => void; cols: number; rows: number; refreshKey: number };
type DetailData = { contract?: any; lessons?: { pending?: any[] }; events?: any[]; governanceFeed?: any[] };

const TAB_LABELS = ["Contract", "Lessons", "Events", "Governance"];

export const Detail = memo(function Detail(p: Props) {
  const { projectName, client, onBack, cols, rows, refreshKey } = p;
  const [data, setData] = useState<DetailData | null>(null);
  const [tab, setTab] = useState(0);
  const [sel, setSel] = useState(0);
  const [item, setItem] = useState<any>(null);

  const textW = Math.max(40, (cols || 80) - 22);
  const maxItems = 12;
  const tabH = (rows || 24) - 5;

  useEffect(() => {
    setData(null); setSel(0); setItem(null);
    (async () => {
      try {
        const [c, l, h, g] = await Promise.all([
          client.callTool("gitgo_contract_show", { project: projectName }).catch(() => null),
          client.callTool("gitgo_lesson_list", { project: projectName }).catch(() => ({ pending: [] })),
          client.callTool("gitgo_history", { project: projectName, limit: 20 }).catch(() => []),
          client.callTool("gitgo_governance_feed", { project: projectName, limit: 20 }).catch(() => []),
        ]);
        setData({ contract: c, lessons: l, events: Array.isArray(h) ? h : [], governanceFeed: Array.isArray(g) ? g : [] });
      } catch { setData({}); }
    })();
  }, [projectName, refreshKey]);

  useInput((_input: string, key: any) => {
    if (item) { if (key.escape) setItem(null); return; }
    if (key.escape) { onBack(); return; }
    if (key.leftArrow)  { setTab((p) => Math.max(0, p - 1)); setSel(0); }
    if (key.rightArrow) { setTab((p) => Math.min(3, p + 1)); setSel(0); }
    if (key.upArrow)    { setSel((p) => Math.max(0, p - 1)); }
    if (key.downArrow) {
      let max = 0;
      if (data) {
        if (tab === 0) max = (data.contract?.decided_features?.length || 0) + (data.contract?.architecture_constraints?.length || 0);
        else if (tab === 1) max = (data.lessons?.pending?.length || 0);
        else if (tab === 2) max = (data.events?.length || 0);
        else if (tab === 3) max = (data.governanceFeed?.length || 0);
      }
      setSel((p) => p < max - 1 ? p + 1 : p);
    }
    if (key.return && data) {
      if (tab === 0 && data.contract) {
        const feats = data.contract.decided_features || [];
        const consts = data.contract.architecture_constraints || [];
        if (sel < feats.length) setItem({ type: "feature", data: feats[sel] });
        else { const ci = sel - feats.length; if (ci >= 0 && ci < consts.length) setItem({ type: "constraint", data: consts[ci] }); }
      } else if (tab === 1 && data.lessons?.pending?.[sel]) setItem({ type: "lesson", data: data.lessons.pending[sel] });
      else if (tab === 2 && data.events?.[sel]) setItem({ type: "event", data: data.events[sel] });
      else if (tab === 3 && data.governanceFeed?.[sel]) setItem({ type: "governance", data: data.governanceFeed[sel] });
    }
  });

  const loaded = data !== null;
  const feats = data?.contract?.decided_features || [];
  const consts = data?.contract?.architecture_constraints || [];
  const lessons = data?.lessons?.pending || [];
  const events = data?.events || [];
  const govFeed = data?.governanceFeed || [];

  if (item) {
    return (
      <Box flexDirection="column" paddingLeft={2} paddingRight={2} paddingTop={1}>
        <Text bold color="cyan">{projectName}</Text>
        <Text dimColor>Esc back</Text>
        <Text bold underline>{(item.type==="lesson"?"Lesson":item.type==="feature"?"Feature":item.type==="constraint"?"Constraint":item.type==="event"?"Event":"Governance")} Detail</Text>
        {item.type==="lesson"&&<LF d={item.data}/>}
        {item.type==="feature"&&<FF d={item.data}/>}
        {item.type==="constraint"&&<CF d={item.data}/>}
        {item.type==="event"&&<EF d={item.data}/>}
        {item.type==="governance"&&<GF d={item.data}/>}
      </Box>);
  }

  const fWin = win(feats, sel, maxItems);
  const cWin = win(consts, sel - feats.length, maxItems);
  const lWin = win(lessons, sel, maxItems);
  const eWin = win(events, sel, maxItems);
  const gWin = win(govFeed, sel, maxItems);
  const ph = (n: number) => Array.from({ length: n }).map((_, i) => sr(`ph-${i}`, false, <Text dimColor>...</Text>));

  return (
    <Box flexDirection="column" paddingLeft={2} paddingRight={2} paddingTop={1} height={tabH}>
      <Text bold color="cyan">{projectName}</Text>
      <Text dimColor>Esc back  ←→ tab  ↑↓ select  Enter detail</Text>
      <Box flexDirection="row">
        {TAB_LABELS.map((label, i) => (
          <Box key={label} paddingLeft={2} paddingRight={2} marginRight={1} backgroundColor={i===tab?"ansi256(240)":undefined}>
            <Text bold={i===tab} color={i===tab?"white":undefined} dimColor={i!==tab}>{label}</Text>
          </Box>
        ))}
      </Box>

      {tab === 0 && (
        <Box flexDirection="column">
          <Text>Tech: {loaded?fmtTech(data.contract?.tech_stack):"..."}</Text>
          <Text bold>Features ({loaded?feats.length:"?"}):</Text>
          {fWin.above}
          {loaded ? fWin.items.map((f,vi) => sr(`f-${fWin.start+vi}`,sel===fWin.start+vi,<Text>[{f.confirmed_count||0}x] {f.name?.slice(0,textW)}</Text>)) : ph(Math.min(maxItems,4))}
          {fWin.below}
          <Text bold>Constraints ({loaded?consts.length:"?"}):</Text>
          {cWin.above}
          {loaded ? cWin.items.map((c,vi) => sr(`c-${cWin.start+vi}`,sel===feats.length+cWin.start+vi,<Text color="red">- {typeof c==="string"?c.slice(0,textW):""}</Text>)) : ph(Math.min(maxItems,2))}
          {cWin.below}
        </Box>
      )}

      {tab === 1 && (
        <Box flexDirection="column">
          <Text bold>Pending Lessons ({loaded?lessons.length:"?"})</Text>
          {lWin.above}
          {loaded ? lWin.items.map((l,vi) => {const idx=lWin.start+vi;const sm:Record<string,string>={critical:"red",high:"red",medium:"yellow"};return sr(`l-${l.id||idx}`,sel===idx,<Text><Text color={sm[l.severity]||undefined}>[{l.severity?.[0]?.toUpperCase()||"?"}]</Text>{" "}{l.category||"?"}: {l.trigger?.slice(0,textW)}</Text>)}) : ph(Math.min(maxItems,4))}
          {lWin.below}
        </Box>
      )}

      {tab === 2 && (
        <Box flexDirection="column">
          <Text bold>Recent Events</Text>
          {eWin.above}
          {loaded ? eWin.items.map((e,vi) => sr(`e-${eWin.start+vi}`,sel===eWin.start+vi,<Text dimColor>{e.timestamp?.slice(0,19)}  {e.operation}  {e.status||""}</Text>)) : ph(Math.min(maxItems,4))}
          {eWin.below}
        </Box>
      )}

      {tab === 3 && (
        <Box flexDirection="column">
          <Text bold>Governance Feed</Text>
          {gWin.above}
          {loaded ? gWin.items.map((g,vi) => {const idx=gWin.start+vi;const oc:Record<string,string>={governance_drift:"red",integrity_warning:"red",policy_check_result:"cyan",workspace_state_snapshot:"green",rejection:"yellow",governance_lesson:"magenta",governance_synced:"green",governance_pushed:"green"};const badge=(g.operation||"").replace("governance_","").replace("workspace_state_","");return sr(`g-${idx}`,sel===idx,<Box flexDirection="row"><Box width={20}><Text dimColor>{g.timestamp?.slice(0,19)||"?"}</Text></Box><Box width={24}><Text color={oc[g.operation]||undefined} bold>{badge}</Text></Box><Box><Text dimColor>{g.status||""}</Text></Box></Box>)}) : ph(Math.min(maxItems,4))}
          {gWin.below}
        </Box>
      )}
    </Box>
  );
});

function sr(key:string,selected:boolean,content:React.ReactNode){return <Box key={key} flexDirection="row"><Box width={2}><Text color={selected?"cyan":undefined}>{selected?"▶":" "}</Text></Box><Box>{content}</Box></Box>}
function win<T>(all:T[],sel:number,max:number){const start=Math.max(0,Math.min(sel-Math.floor(max/2),Math.max(0,all.length-max)));const items=all.slice(start,start+max);const above=start>0?<Box><Box width={2}><Text> </Text></Box><Box><Text dimColor>... {start} more above ...</Text></Box></Box>:null;const below=start+max<all.length?<Box><Box width={2}><Text> </Text></Box><Box><Text dimColor>... {all.length-start-max} more below ...</Text></Box></Box>:null;return{start,items,above,below}}
function fmtTech(ts:any){return Array.isArray(ts)?ts.join(", "):(ts||"(none)")}
function LF({d}:{d:any}){return<Box flexDirection="column"><FL k="Severity" v={d.severity||"?"}/><FL k="Category" v={d.category||"?"}/><FL k="Trigger" v={d.trigger||""}/><FL k="TechStack" v={fmtTech(d.tech_stack)}/><FL k="Verified" v={`${d.verified_count||0} times`}/><FL k="Created" v={d.created_at?.slice(0,19)||"?"}/>{d.description&&<Box><Text bold>Description:</Text><Text>{d.description.slice(0,200)}</Text></Box>}{d.id&&<Box><Text dimColor>ID: {d.id}</Text></Box>}</Box>}
function FF({d}:{d:any}){return<Box flexDirection="column"><FL k="Name" v={d.name||"?"}/><FL k="Location" v={d.location||"(none)"}/><FL k="Confirmed" v={`${d.confirmed_count||0} times`}/>{d.description&&<Box><Text bold>Description:</Text><Text>{d.description.slice(0,200)}</Text></Box>}</Box>}
function CF({d}:{d:any}){return<Box flexDirection="column"><Box><Text bold>Constraint:</Text><Text color="red">{typeof d==="string"?d:d.name||JSON.stringify(d)}</Text></Box>{d.description&&<Box><Text bold>Description:</Text><Text>{d.description.slice(0,200)}</Text></Box>}</Box>}
function EF({d}:{d:any}){return<Box flexDirection="column"><FL k="Timestamp" v={d.timestamp?.slice(0,19)||"?"}/><FL k="Operation" v={d.operation||"?"}/><FL k="Status" v={d.status||"?"}/>{d.detail&&<Box><Text bold>Detail:</Text><Text dimColor>{JSON.stringify(d.detail).slice(0,200)}</Text></Box>}{d.commit&&<FL k="Commit" v={d.commit?.slice(0,12)||"?"}/>}</Box>}
function GF({d}:{d:any}){return<Box flexDirection="column"><FL k="Timestamp" v={d.timestamp?.slice(0,19)||"?"}/><FL k="Operation" v={d.operation||"?"}/><FL k="Status" v={d.status||"?"}/>{d.correlation_id&&<FL k="Session" v={d.correlation_id.slice(0,12)||"?"}/>}{d.detail&&<Box><Text bold>Detail:</Text>{typeof d.detail==="object"?Object.entries(d.detail).map(([k,v])=><Box key={k} flexDirection="row" paddingLeft={2}><Box width={18}><Text dimColor>{k}:</Text></Box><Box><Text dimColor>{JSON.stringify(v).slice(0,80)}</Text></Box></Box>):<Text dimColor>{JSON.stringify(d.detail).slice(0,200)}</Text>}</Box>}</Box>}
function FL({k,v}:{k:string;v:string}){return<Box flexDirection="row"><Box width={14}><Text dimColor>{k}:</Text></Box><Box><Text>{v.slice(0,60)}</Text></Box></Box>}
