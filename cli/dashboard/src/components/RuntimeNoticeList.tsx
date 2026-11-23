import React, {useEffect,useState} from "react";
import { Box, Text } from "@anthropic/ink";
import type { RuntimeNotice } from "../types.js";
import {geometry} from "../theme/tokens.js";
import {useCopy} from "../ui/PageFrame.js";
import {Disclosure} from "./Disclosure.js";
import {TraceContent} from "./TraceContent.js";
import {DecisionPanel} from "./DecisionPanel.js";
import type {TraceBodyReader} from "../chat/traceSource.js";
import {runtimeNoticeFromEvent} from "../daemon/runtimeNotices.js";
import {actorLabelCopy} from "../daemon/agentLabels.js";
import type {StreamEvent} from "../daemon/streamEvents.js";
import {useDisclosureState} from "../hooks/useDisclosureState.js";

function NoticeDetail({notice,readBody}: {notice:RuntimeNotice;readBody?:TraceBodyReader}) {
  const t=useCopy();
  return <TraceContent references={notice.trace_refs} read={readBody}>{events=><TraceContent detailRef={notice.detail_ref || ""} readDetail={readBody?.detail}>
    {(saved:unknown)=>{
      const original=saved && typeof saved === "object" && "legacy_record" in saved ? saved.legacy_record : events?.[0];
      const full=original ? runtimeNoticeFromEvent(original as StreamEvent,true)?.detail : undefined;
      return <Text dimColor>{full || notice.detail || t({en:"No saved detail",zh:"没有保存的详情"})}</Text>;
    }}
  </TraceContent>}</TraceContent>;
}

export function RuntimeNoticeList({
  notices, verbose = true,
}: { notices: RuntimeNotice[]; verbose?: boolean }) {
  const t=useCopy();
  if (notices.length === 0) return null;
  // runtimeNotices already filters routine control-plane churn.  What remains
  // is actionable or semantically meaningful in both compact and verbose mode;
  // verbose only reveals its trace detail.
  const visible = notices;
  return (
    <Box flexDirection="column" paddingLeft={2} marginTop={1}>
      {verbose ? <Text dimColor>{t({en:"trajectory",zh:"轨迹"})}</Text> : null}
      {visible.map((notice) => <RuntimeNoticeRow key={notice.key} notice={notice} verbose={verbose}/>)}
    </Box>
  );
}

export function RuntimeNoticeRow({
  notice, verbose = true,readBody,
}: { notice: RuntimeNotice; verbose?: boolean;readBody?:TraceBodyReader }) {
  const t=useCopy();
  const label=notice.label_copy ? t(notice.label_copy) : notice.label;
  const actor=notice.actor_label ? t(actorLabelCopy(notice.actor_label)) : "";
  const [expanded,toggle]=useDisclosureState(false,'notice:'+notice.key);
  if(notice.decision)return <DecisionPanel decision={notice.decision} answer={notice.decision_answer} active={false}
    selectedIndex={0} composing={false} submitting={false} expanded={expanded} onExpandedChange={toggle}/>;
  // Terminal lifecycle facts are plain status text within the round capsule.
  // Preserve failure detail, without introducing another disclosure or marker.
  if(notice.kind === "agent_complete" || notice.kind === "agent_terminal") return <Box flexDirection="column">
    <Text dimColor>{actor ? actor + " · " : ""}{label}</Text>
    {notice.severity === "error" || notice.severity === "warning" ? <Box paddingLeft={geometry.record.indent}>
      <NoticeDetail notice={notice} readBody={readBody}/>
    </Box> : null}
  </Box>;
  const expandable=Boolean(notice.detail || notice.trace_refs?.length || notice.detail_ref);
  return <Disclosure expanded={expanded && expandable} expandable={expandable} onToggle={()=>toggle(current=>!current)} label={<>
    {actor ? <Text dimColor>{actor} · </Text> : null}
    <Text bold={notice.severity === "warning" || notice.severity === "error"}>{label}</Text>
  </>}>
    {expanded && expandable ? <Box paddingLeft={geometry.record.indent} flexDirection="column">
      <NoticeDetail notice={notice} readBody={readBody}/>
    </Box> : null}
  </Disclosure>;
}
