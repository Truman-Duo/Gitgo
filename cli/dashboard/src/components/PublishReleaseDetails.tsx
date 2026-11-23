import React, {useCallback, useEffect, useLayoutEffect, useRef, useState} from "react";
import {randomUUID} from "node:crypto";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {publishCommand, publishItem, publishReleaseStatus, type PublishItem, type PublishReleaseStatus, type ReleasePhase} from "../backend/publish.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {useManagedInput, useScrollInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {useCopy} from "../ui/PageFrame.js";
import {PageActions} from "../ui/PageActions.js";
import type {Copy} from "../ui/pageContract.js";
import {colors, usePanelSize} from "../theme/index.js";
import {SelectionRow, SelectionText} from "./Selection.js";
import {FeedbackMessage} from "./FeedbackMessage.js";
import {PublishOperationDraft} from "./PublishOperationDraft.js";
import {PublishCISelection} from "./PublishCISelection.js";
import type {FooterConfig} from "./CommandBar.js";

const clean = (value: string) => value.replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, " ");
const activeStates = new Set(["queued", "running", "stopping"]);
const phaseCopy: Record<ReleasePhase, Copy> = {
  prepared: {en: "Preparation", zh: "准备"}, draft: {en: "Remote draft", zh: "远端草稿"},
  published: {en: "Publication", zh: "正式发布"}, verified: {en: "Delivery verification", zh: "交付核对"},
};
const stateCopy: Record<string, Copy> = {
  queued: {en: "Queued", zh: "排队中"}, running: {en: "Running", zh: "运行中"}, stopping: {en: "Stopping", zh: "正在停止"},
  completed: {en: "Requested phases completed", zh: "所选阶段已完成"}, success: {en: "Passed", zh: "已通过"},
  cancelled: {en: "Cancelled", zh: "已取消"}, failed: {en: "Failed", zh: "失败"}, stopped: {en: "Stopped; result unconfirmed", zh: "已停止，结果未确认"},
  blocked: {en: "Blocked", zh: "受阻"}, needs_reconciliation: {en: "Reconciliation required", zh: "需要对账"},
  unverified: {en: "Unverified", zh: "未核对"}, partial: {en: "Partially observed", zh: "仅核对部分结果"},
  released: {en: "Published", zh: "已发布"}, published: {en: "Published", zh: "已发布"}, draft: {en: "Draft", zh: "草稿"},
  absent: {en: "Absent", zh: "不存在"},
  reused:{en:"Reused",zh:"已复用"},verified:{en:"Verified",zh:"已核对"},
  observed:{en:"Observed only",zh:"仅核对观察"},
};
type Pending = {operation: string; args: Record<string, unknown>; id: string};
type Row = {id: string; label: string; detail?: string; run: () => void};

/** One fixed package. Reads only observe; explicit commands use the existing Publish boundary. */
export function PublishReleaseDetails({client, project, initialItem, packageId, localState, onFooter, onDismiss}: {
  client: BackendClient; project: string; initialItem: PublishItem; packageId: string; localState: PublishLocalState;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
}) {
  const t = useCopy(), {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [item, setItem] = useState(initialItem), [status, setStatus] = useState<PublishReleaseStatus | null>(null);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [form, setForm] = useState(false);
  const [selected, setSelected] = useState("plan"), [expanded, setExpanded] = useState("");
  const [confirm, setConfirm] = useState<{phase: ReleasePhase; revision: number; plan: string} | null>(null), [choice, setChoice] = useState(0);
  const [uncertain, setUncertain] = useState(false);
  const [choosingCI, setChoosingCI] = useState(false);
  const [signing, setSigning] = useState(false),[savedKeyId,setSavedKeyId]=useState("");
  const scroll = useRef<ScrollBoxHandle>(null), selectedElement = useRef<DOMElement>(null);
  const mounted = useRef(true), generation = useRef(0), reading = useRef(false), mutating = useRef(false);
  const cursor = useRef(0), lastJob = useRef(""), pending = useRef<Pending | null>(null);
  const feedback = useFeedback({kind: "page", id: `publish/release/${project}/${initialItem.id}/${packageId}`});
  const readFeedback = useFeedback({kind: "page", id: `publish/release-read/${project}/${initialItem.id}/${packageId}`},
    {tone: "error"});
  const help = useLocalHelpCommand("publish.release", onFooter, !form&&!choosingCI);
  const pkg = item.packages?.find(value => value.id === packageId), control = item.release_controller, job = control?.job;
  const samePackage = control?.plan.package_id === packageId;
  const active = activeStates.has(job?.state ?? "");
  const state = (value?: string) => value ? t(stateCopy[value] ?? {en: value, zh: value}) : t({en: "Not run", zh: "尚未运行"});
  const seconds=(value:number|null|undefined)=>value==null?t({en:"Unrecorded",zh:"未记录"}):`${value.toFixed(2)} s`;
  const eventLabel=(name:string)=>name.startsWith("phase-")&&phaseCopy[name.slice(6) as ReleasePhase]?t(phaseCopy[name.slice(6) as ReleasePhase])
    :name==="run"?t({en:"Task",zh:"任务"}):name==="feeds"?t({en:"Update feed",zh:"更新源"})
    :name==="signed-assets"?t({en:"Signed assets",zh:"签名产物"})
    :/^check-\d+$/.test(name)?`${t({en:"CI checks",zh:"CI 检查"})} ${name.slice(6)}`
    :/^artifact-\d+$/.test(name)?`${t({en:"Artifact",zh:"产物"})} ${name.slice(9)}`:name;

  const read = useCallback(async (force = false) => {
    if (reading.current && !force) return;
    const id = ++generation.current; reading.current = true;
    if (mounted.current) setLoading(true);
    try {
      const fresh = await publishItem(client, project, initialItem.id);
      if (!mounted.current || generation.current !== id) return;
      setItem(fresh);
      const saved = fresh.release_controller;
      if (!saved || saved.plan.package_id !== packageId) {setStatus(null); readFeedback.clear(); return;}
      setStatus(previous => previous?.control.plan.binding === saved.plan.binding && previous.control.job?.id === saved.job?.id ? previous : null);
      if ((saved.job?.id ?? "") !== lastJob.current) {cursor.current = 0; lastJob.current = saved.job?.id ?? "";}
      const next = await publishReleaseStatus(client, project, fresh.id, saved.job?.id, cursor.current);
      if (!mounted.current || generation.current !== id) return;
      if (next.control.plan.binding !== saved.plan.binding) throw new Error("PUBLISH_RELEASE_PLAN_STALE");
      setStatus(next); readFeedback.clear();
    } catch (error: any) {
      if (mounted.current && generation.current === id) readFeedback.show(String(error?.code ?? error?.message ?? error));
    } finally {
      if (generation.current === id) {reading.current = false; if (mounted.current) setLoading(false);}
    }
  }, [client, project, initialItem.id, packageId, readFeedback.show, readFeedback.clear]);
  useEffect(() => {mounted.current = true; void read(); return () => {mounted.current = false; generation.current++;};}, [read]);
  useEffect(() => {
    if (!active || form || choosingCI || confirm) return;
    const timer = setInterval(() => {if (!mutating.current) void read();}, 1000);
    return () => clearInterval(timer);
  }, [active, form, choosingCI, Boolean(confirm), read]);
  useEffect(() => {if (!form && !choosingCI && !help.active) onFooter({hidden: true}); return () => {if (!form&&!choosingCI) onFooter(null);};}, [form, choosingCI, help.active, onFooter]);

  const command = async (operation: string, values: Record<string, unknown>, retry = false) => {
    if (mutating.current) return;
    const call = retry ? pending.current : {operation, args: operation==="signing.save"?values:{item_id: item.id, expected_revision: item.revision, ...values}, id: randomUUID()};
    if (!call) return;
    mutating.current = true; pending.current = call; setBusy(true);
    try {
      const result = await publishCommand(client, project, call.operation, call.args, call.id);
      if (!mounted.current) return;
      pending.current = null; setUncertain(false); setConfirm(null);
      if (result.item) setItem(result.item);
      if(call.operation==="signing.save"){setSavedKeyId(String(call.args.key_id??""));localState.remove(`release-signing/${item.id}/${packageId}`);setForm(false);setSigning(false);}
      if (call.operation === "release.plan") {localState.remove(`release-plan/${item.id}/${packageId}`); setForm(false); setChoosingCI(false);}
      feedback.show(t(call.operation === "signing.save"?{en:"Project signing key saved encrypted.",zh:"项目签名密钥已加密保存。"}:call.operation === "release.plan" ? {en: "Release plan saved.", zh: "发布计划已保存。"}
        : call.operation === "release.cancel" ? {en: "Stop request saved; inspect the task's final state.", zh: "停止请求已保存，请核对任务最终状态。"}
        : {en: "Request saved. The task's recorded state shows its progress.", zh: "请求已保存，进度以任务记录为准。"}), {tone: "info"});
      await read(true);
    } catch (error: any) {
      if (!mounted.current) return;
      const known = error?.effect_state === "not_committed";
      if (known) pending.current = null;
      setUncertain(!known); setConfirm(null); if (!known) {setForm(false);setChoosingCI(false);}
      feedback.show(`${String(error?.code ?? error?.message ?? error)}${known ? "" : t({en: " · The receipt is unconfirmed. Re-read it before starting another request.", zh: " · 回执未确认，请先重读同一请求的结果。"})}`, {tone: "error"});
      await read(true);
    } finally {mutating.current = false; if (mounted.current) setBusy(false);}
  };
  const requestPhase = (phase: ReleasePhase) => {setChoice(0); setConfirm({phase, revision: item.revision!, plan: control!.plan.binding});};
  const rows: Row[] = [];
  if (pkg && item.allowed_actions.includes("release_plan") && !uncertain && (!samePackage||status)) rows.push({id: "plan", label: t({en: samePackage ? "Edit release plan" : "Prepare release plan", zh: samePackage ? "编辑发布计划" : "准备发布计划"}), run: () => {setSigning(false);setForm(true);}});
  if (samePackage) {
    if(control!.plan.capabilities.native_update_recipe&&!active&&!uncertain&&item.allowed_actions.includes("release_plan"))
      rows.push({id:"signing",label:t({en:"Configure project signing key",zh:"配置项目签名密钥"}),run:()=>{setSigning(true);setForm(true);}});
    if (status?.plan.capabilities.ci_acquisition && item.allowed_actions.includes("release_plan") && !uncertain && !active)
      rows.push({id:"ci",label:t({en:"Select CI artifacts",zh:"选择 CI 产物"}),run:()=>setChoosingCI(true)});
    for (const artifact of status?.plan.ci_artifacts ?? []) rows.push({id:`ci-${artifact.artifact_id}`,
      label:`${artifact.name} · run ${artifact.run_id} / attempt ${artifact.attempt} · ${artifact.size} B`,
      detail:expanded===`ci-${artifact.artifact_id}`?artifact.sha256:undefined,
      run:()=>setExpanded(value=>value===`ci-${artifact.artifact_id}`?"":`ci-${artifact.artifact_id}`)});
    for (const phase of control!.plan.supported_phases) {
      rows.push({id: phase, label: `${t(phaseCopy[phase])} · ${state(job?.phases?.[phase]?.state)}`, run: () => setExpanded(value => value === phase ? "" : phase)});
      if (expanded === phase) {
        const result = job?.phases?.[phase];
        rows.at(-1)!.detail = [result?.reason, result?.seconds === undefined ? "" : `${result.seconds.toFixed(2)} s`].filter(Boolean).join(" · ");
        if (pkg && item.allowed_actions.includes("release_run") && !uncertain && status && !status.stale) rows.push({id: `run-${phase}`, label: t(phase === "prepared" ? {en: "Run preparation", zh: "执行准备"}
          : phase === "draft" ? {en: "Upload remote draft…", zh: "上传远端草稿…"}
          : phase === "verified" ? {en:"Verify anonymous delivery…",zh:"核对匿名交付…"}
          : {en: "Publish this version…", zh: "发布这个版本…"}), run: () => requestPhase(phase)});
      }
    }
  }
  for (const asset of pkg?.assets ?? []) rows.push({id: `asset-${asset.name}`, label: `${asset.name} · ${asset.size} B`,
    detail: expanded === `asset-${asset.name}` ? asset.sha256 : undefined, run: () => setExpanded(value => value === `asset-${asset.name}` ? "" : `asset-${asset.name}`)});
  if (job && item.allowed_actions.includes("release_cancel") && job.state !== "stopping" && !uncertain) rows.push({id: "cancel", label: t({en: "Stop this release task", zh: "停止本次发布任务"}), run: () => void command("release.cancel", {job_id: job.id})});
  if (job && item.allowed_actions.includes("release_reconcile") && !uncertain) rows.push({id: "reconcile", label: t({en: "Reconcile saved remote results", zh: "对账已保存的远端结果"}), run: () => void command("release.reconcile", {job_id: job.id})});
  if (uncertain) rows.push({id: "retry", label: t({en: "Re-read the same request receipt", zh: "重读同一请求的回执"}), run: () => void command("", {}, true)});
  rows.push({id: "refresh", label: t({en: "Refresh saved state", zh: "刷新已保存状态"}), run: () => void read(true)});
  if (status?.has_more) rows.push({id: "more", label: t({en: "Read next event page", zh: "读取后续事件记录"}), run: () => {cursor.current = status.next_sequence; void read(true);}});
  if (cursor.current) rows.push({id: "first", label: t({en: "Return to first event page", zh: "返回首批事件记录"}), run: () => {cursor.current = 0; void read(true);}});
  const at = Math.max(0, rows.findIndex(row => row.id === selected));
  useLayoutEffect(() => {if (selectedElement.current) scroll.current?.scrollIntoView(selectedElement.current);}, [selected, expanded, w, h]);
  useScrollInput(scroll, !form&&!choosingCI);
  useManagedInput((text, key) => {
    if (help.handle(text, key)) return;
    if (matchChord("escape", text, key)) {if (confirm) setConfirm(null); else onDismiss(); return;}
    if (busy) return;
    if (confirm) {
      if (matchChord("left", text, key)) setChoice(0);
      else if (matchChord("right", text, key)) setChoice(1);
      else if (matchChord("enter", text, key)) {
        if (choice === 0) setConfirm(null);
        else void command("release.run", {through: confirm.phase, expected_revision: confirm.revision});
      }
      return;
    }
    if (matchChord("up", text, key)) setSelected(rows[Math.max(0, at - 1)]!.id);
    else if (matchChord("down", text, key)) setSelected(rows[Math.min(rows.length - 1, at + 1)]!.id);
    else if (matchChord("enter", text, key) && !loading) rows[at]?.run();
  }, {isActive: !form&&!choosingCI, clipboard: false});

  if (choosingCI && status && control) return <PublishCISelection key={`${project}/${item.id}/${control.plan.binding}`}
    client={client} project={project} itemId={item.id} planBinding={control.plan.binding} sourceOid={status.plan.source_oid??pkg?.source_oid??""}
    initial={status.plan.ci_artifacts??[]} saving={busy} onFooter={onFooter} onCancel={()=>setChoosingCI(false)}
    onSubmit={ci_artifacts=>void command("release.plan",{package_id:packageId,target:control.plan.target,notes:status.plan.notes??"",ci_artifacts,
      delivery_recipe:status.plan.delivery_recipe??"project",signing_key_id:status.plan.native?.signing_key_id??"",upgrade_route:status.plan.native?.upgrade_route??"direct"})}/>;

  if (form) {
    const target = control?.plan.target ?? item.collaboration?.snapshots.at(-1)?.target;
    return <PublishOperationDraft key={`${signing?"signing":"plan"}/${item.id}/${packageId}`} mode={signing?"signing":"release_plan"} item={item} busy={busy} status={feedback.text}
      allowNativeRelease={Boolean(control?.plan.capabilities.native_update_recipe)}
      localState={localState} formKey={`${signing?"release-signing":"release-plan"}/${item.id}/${packageId}`} initialValues={{...target, package_id: packageId,notes:status?.plan.notes??"",
        delivery_recipe:status?.plan.delivery_recipe??"project",key_id:status?.plan.native?.signing_key_id??savedKeyId,
        signing_key_id:status?.plan.native?.signing_key_id??savedKeyId,upgrade_route:status?.plan.native?.upgrade_route??"direct"}}
      onFooter={onFooter} onDismiss={() => setForm(false)} onSubmit={(operation, args) => void command(operation,
        operation==="release.plan"?{...args,...(["kind","instance","repository","account"].every(key=>(args.target as Record<string,unknown>)?.[key]===(control?.plan.target as Record<string,unknown>)?.[key])
          ?{ci_artifacts:(status?.plan.ci_artifacts??[]).map(({run_id,attempt,artifact_id})=>({run_id,attempt,artifact_id}))}:{})}:args)}/>;
  }
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{t({en: "Version", zh: "版本"})} · {clean(pkg?.version ?? packageId)}</Text>
    <Text dimColor wrap="truncate-end">{clean(pkg?.source_oid ?? "")} · {clean(pkg?.tree_oid ?? "")}</Text>
    {loading || busy ? <Text dimColor>{t(busy ? {en: "Saving request…", zh: "正在保存请求…"} : {en: "Reading saved state…", zh: "正在读取已保存状态…"})}</Text> : null}
    <FeedbackMessage feedback={feedback.message}/>
    <FeedbackMessage feedback={readFeedback.message}/>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {!pkg ? <Text color={colors.danger}>{t({en: "This package record is unavailable.", zh: "此产物记录不可用。"})}</Text> : null}
      {control && !samePackage ? <Text>{t({en: "The item's current release plan belongs to another package.", zh: "事项当前的发布计划属于另一个产物。"})}</Text> : null}
      {samePackage ? <>
        <Text>{clean(control!.plan.target.repository)} · {clean(control!.plan.target.account)} · {clean(control!.plan.target.instance)}</Text>
        <Text dimColor wrap="truncate-end">{t({en: "Plan", zh: "计划"})} {control!.plan.binding}</Text>
        {!status && !loading ? <Text dimColor>{t({en: "Plan details not loaded. Refresh to read them.", zh: "计划详情尚未读取，可刷新以读取。"})}</Text> : null}
        {job ? <Text>{state(job.state)} · {t(phaseCopy[job.through])}{job.observe_only ? t({en: " · Observation only", zh: " · 仅对账观察"}) : ""}</Text> : null}
        {job?.elapsed_seconds!==undefined?<Text dimColor>{t({en:"Task elapsed",zh:"任务耗时"})} {seconds(job.elapsed_seconds)} · {t({en:"Queue",zh:"排队"})} {seconds(job.queue_seconds)}</Text>:null}
        {job?.reason ? <Text color={colors.danger}>{clean(job.reason)}</Text> : null}
        {job?.transfer ? <Text dimColor>{t(job.transfer.direction === "upload" ? {en: "Sending bytes", zh: "发送字节"} : {en: "Reading bytes", zh: "读取字节"})} · {clean(job.transfer.asset)} · {job.transfer.bytes} B{job.transfer.total === null ? "" : ` / ${job.transfer.total} B`} · {job.transfer.state === "completed" ? t({en: "Transfer completed", zh: "传输完成"}) : job.transfer.state === "stopped" ? t({en: "Transfer stopped", zh: "传输已停止"}) : state(job.transfer.state)}</Text> : null}
        {job?.transfer?<Text dimColor>{t({en:"Transfer elapsed",zh:"传输耗时"})} {seconds(job.transfer.seconds)} · {t({en:"Observed average",zh:"平均吞吐"})} {job.transfer.seconds>0?`${Math.round(job.transfer.bytes/job.transfer.seconds)} B/s`:t({en:"Unrecorded",zh:"未记录"})}</Text>:null}
        {job?.remote_state ? <Text>{t({en: "Remote observation", zh: "远端观察"})} · {state(job.remote_state)}</Text> : null}
        {status?.plan.native?<>
          <Text>{t({en:"Gitgo native update",zh:"Gitgo 原生更新"})} · {status.plan.native.channel} · {t({en:"Sequence",zh:"序列"})} {status.plan.native.sequence} · {clean(status.plan.native.signing_key_id)}</Text>
          <Text dimColor>{status.plan.native.targets.join(" · ")}</Text>
          <Text>{t({en:"Update feed",zh:"更新源"})} · {job?.feed_state==="advanced"?t({en:"Advanced",zh:"已推进"}):t({en:"Not verified",zh:"尚未核对"})}</Text>
        </>:null}
        {job?.release_url ? <Text>{clean(job.release_url)}</Text> : null}
        {status?.stale ? <Text color={colors.danger}>{t({en: "The plan is stale. Prepare a current plan before running it.", zh: "计划已过期，需准备当前计划后才能执行。"})}</Text> : null}
        {!control!.plan.capabilities.ci_acquisition ? <Text dimColor>{t({en:"CI acquisition is unavailable for this platform adapter.",zh:"当前平台适配器不提供 CI 产物获取。"})}</Text> : null}
        {!control!.plan.capabilities.anonymous_delivery_verification ? <Text dimColor>{t({en:"Anonymous delivery verification is unavailable for this platform adapter.",zh:"当前平台适配器不提供匿名交付核对。"})}</Text> : null}
        {!control!.plan.capabilities.signed_update_feed ? <Text dimColor>{t({en:"This project recipe does not advance a signed update feed.",zh:"当前项目配方不推进签名更新源。"})}</Text> : null}
        {status?.plan.body ? <Text>{clean(status.plan.body)}</Text> : null}
      </> : null}
      {confirm ? <>
        <Text>{t(phaseCopy[confirm.phase])} · {clean(control!.plan.target.repository)} · {clean(control!.plan.target.account)}</Text>
        <Text>{t(confirm.phase === "prepared" ? {en: "Validate the fixed source and remote identity without changing the remote?", zh: "核对固定来源与远端身份，不修改远端？"}
          : confirm.phase === "draft" ? {en: "Create or reuse the fixed remote tag and upload the release draft?", zh: "创建或复用固定远端标签，并上传发布草稿？"}
          : confirm.phase === "verified" ? {en:"Advance any unfinished publication phases, then verify public bytes without account credentials?",zh:"完成尚未完成的发布阶段，再以匿名请求核对公开下载字节？"}
          : status?.plan.native ? {en:"Publish this fixed version and replace the displayed channel's four-target signed update feed?",zh:"发布此固定版本，并替换上述频道的四目标签名更新源？"}
          : {en: "Publish this fixed version and its assets to the displayed repository?", zh: "将此固定版本及产物正式发布到上述仓库？"})}</Text>
        <PageActions items={[{id: 0, enabled: !busy, label: {en: "Back", zh: "返回"}}, {id: 1, enabled: !busy, label: {en: "Confirm", zh: "确认执行"}}]} selected={choice} focused/>
      </> : rows.map((row, index) => <Box key={row.id} flexDirection="column" ref={index === at ? selectedElement : undefined}>
        <SelectionRow selected={index === at}><SelectionText wrap="wrap">{clean(row.label)}</SelectionText></SelectionRow>
        {row.detail ? <Text dimColor>{clean(row.detail)}</Text> : null}
      </Box>)}
      {(status?.events ?? []).map(event => <Box key={event.sequence} flexDirection="column">
        <Text dimColor>{clean(eventLabel(event.name))} · {state(event.state)}{event.seconds===undefined?"":` · ${seconds(event.seconds)}`}{event.reason || event.code || event.error ? ` · ${clean(event.reason || event.code || event.error || "")}` : ""}</Text>
        {event.result?.timing?<Text dimColor>{t({en:"CI observed interval",zh:"CI 观察区间"})} {seconds(event.result.timing.observed_job_span_seconds)} · {t({en:"Runner occupancy",zh:"Runner 占用"})} {seconds(event.result.timing.runner_occupied_seconds)} · {t({en:"Queue",zh:"排队"})} {seconds(event.result.timing.queue_seconds)}</Text>:null}
      </Box>)}
    </ScrollBox>
  </Box>;
}
