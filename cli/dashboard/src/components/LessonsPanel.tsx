import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,standardKey,moveSelection,reconcileSelection} from "../ui/pageContract.js";
import {PageActions} from "../ui/PageActions.js";
import React, {memo, useState, useEffect, useCallback, useRef} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {lessonHarvestPreview, lessonHarvestResolve, lessonHarvestList, lessonHarvestContext, lessonList, lessonSearch, lessonVerify} from "../backend/tools.js";
import {resolveLessonsKey} from "../input/overlays/lessons.js";
import {colors, usePanelSize, useSelectionStyle} from "../theme/index.js";
import {LessonsTab} from "./LessonsTab.js";
import {BtwPanel} from "./BtwPanel.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {applyTextOp} from "../hooks/useTextInput.js";
import type {FooterConfig} from "./CommandBar.js";
import {EDIT_FIELDS, fieldValue, proposalReviews, reviseField, reviewsForSubmit, type CandidateReview} from "./lessonReview.js";
type Props = {client: BackendClient; project: string; cols: number; initialQuery?: string; onDismiss: () => void;
  interactive?: boolean; cmdInput?: UseTextInputReturn; onFooter?: (config: FooterConfig | null) => void};
const FIELD_LABELS={
 trigger:{en:"Trigger",zh:"触发条件"},rule:{en:"Rule",zh:"规则"},knowledge_type:{en:"Type",zh:"类型"},
 conditions:{en:"Conditions",zh:"适用条件"},validation:{en:"Validation",zh:"验证"},
 novelty:{en:"Novelty",zh:"新颖性"},limitations:{en:"Limitations",zh:"限制"}
} as const;
type Mode = "list" | "search" | "harvest" | "confirm" | "edit" | "evidence" | "discuss";

export const LessonsPanel = memo(function LessonsPanel({client, project, initialQuery, onDismiss,
  interactive = true, cmdInput, onFooter}: Props) {
  const t=useCopy();
  const [actionIndex,setActionIndex]=useState(0),[actionsFocused,setActionsFocused]=useState(true);
  const [lessons, setLessons] = useState<any>(null);
  const [proposals, setProposals] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<Mode>(initialQuery ? "search" : "list");
  const [query, setQuery] = useState(initialQuery || "");
  const [searchResults, setSearchResults] = useState<any[] | null>(null);
  const [sel, setSel] = useState(0);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState<any>(null);
  const [reviews, setReviews] = useState<CandidateReview[]>([]);
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [fieldIndex, setFieldIndex] = useState(0);
  const [evidence, setEvidence] = useState<any>(null);
  const [sidecarId, setSidecarId] = useState("");
  const scrollRef = useRef<ScrollBoxHandle>(null);
  const refreshSequence = useRef(0);
  const mounted = useRef(true);
  const {w} = usePanelSize({minWidth: 40, widthOffset: 6});
  useScrollInput(scrollRef, interactive && !["edit", "harvest", "discuss"].includes(mode));
  const refresh = useCallback(() => {
    const seq = ++refreshSequence.current;
    return Promise.all([lessonList(client, project), lessonHarvestList(client, project)])
      .then(([nextLessons, nextProposals]: any[]) => {
        if (!mounted.current || seq !== refreshSequence.current) return;
        setLessons(nextLessons); setProposals(nextProposals?.proposals || []); setLoading(false);
      }).catch(error => {
        if (!mounted.current || seq !== refreshSequence.current) return;
        setStatus(String(error?.message || error)); setLoading(false);
      });
  }, [client, project]);
  useEffect(() => {
    mounted.current = true;
    const unsubscribe = client.subscribeEvents?.(event => {
      if (event.project === project && ["lesson_harvest_proposed", "lesson_harvest_resolved", "lessons_harvested"].includes(event.event)) void refresh();
    });
    void refresh();
    return () => { mounted.current = false; refreshSequence.current++; unsubscribe?.(); };
  }, [client, project, refresh]);
  useEffect(() => {
    if (!onFooter) return;
    if (!cmdInput || !["harvest", "edit"].includes(mode)) {
      onFooter({hidden: true}); return () => onFooter(null);
    }
    onFooter({kind: "normal", cmdInput, suggestions: [], suggestionIdx: 0, cmdResult: "",
      statusText: mode === "edit" ? t({en:"Editing ",zh:"正在编辑 "}) + t(FIELD_LABELS[EDIT_FIELDS[fieldIndex]!]) + t({en:" · Tab next field · Enter keep draft · Esc cancel",zh:" · Tab 下一字段 · Enter 保留草稿 · Esc 取消"})
        : t({en:"Enter analyze · Esc cancel",zh:"Enter 分析 · Esc 取消"})});
    return () => onFooter(null);
  }, [mode, fieldIndex, cmdInput, cmdInput?.value, cmdInput?.cursor, onFooter]);
  useEffect(() => {
    if (initialQuery?.trim()) void lessonSearch(client, project, initialQuery.trim())
      .then((r: any) => setSearchResults(Array.isArray(r) ? r : r?.lessons || []))
      .catch(error => setStatus(String(error?.message || error)));
  }, [initialQuery, client, project]);
  const openProposal = (value: any) => {
    setProposal(value); setReviews(proposalReviews(value)); setCandidateIndex(0);setActionIndex(0);setActionsFocused(false);
    setMode("confirm"); setStatus(""); scrollRef.current?.scrollTo(0);
  };
  const proposalRef = () => ({proposal_id: proposal.proposal_id,
    proposal_version: proposal.proposal_version, candidate_index: candidateIndex});
  const submit = (action: "accept" | "discard") => {
    if (busy || !proposal) return;
    setBusy(true); setStatus("");
    void lessonHarvestResolve(client, project, proposal.proposal_id, action, proposal.proposal_version,
      action === "accept" ? reviewsForSubmit(reviews) : undefined).then((value: any) => {
      setStatus(value.status === "accepted" ? t({en:"Saved ",zh:"已保存 "}) + value.count + t({en:" reviewed lesson(s).",zh:" 条已审阅经验。"}) : t({en:"Proposal discarded.",zh:"已放弃提案。"}));
      setMode("list"); setProposal(null); void refresh();
    }).catch(error => setStatus(String(error?.message || error) + t({en:" · Return to the list and reopen if the proposal changed.",zh:" · 如提案已变更，请返回列表重新打开。"})))
      .finally(() => setBusy(false));
  };
  const keepField = () => {
    if (!cmdInput) return false;
    try {
      const revised = reviseField(reviews[candidateIndex]!, EDIT_FIELDS[fieldIndex]!, cmdInput.value);
      setReviews(current => current.map((item, index) => index === candidateIndex ? revised : item));
      setStatus(""); return revised;
    } catch (error: any) { setStatus(error.message); return false; }
  };
  const listActions=[
    {id:0,enabled:Boolean(proposals[0]),label:{en:"Proposals",zh:"候选提案"}},
    {id:1,enabled:true,label:{en:"Search",zh:"搜索"}},
    {id:2,enabled:Boolean(cmdInput),label:{en:"Harvest",zh:"提炼经验"}},
    {id:3,enabled:Boolean(lessons?.pending?.[sel]),label:{en:"Review legacy",zh:"审阅既有经验"}},
  ];
  const reviewActions=[
    {id:0,enabled:Boolean(cmdInput),label:{en:"Edit",zh:"编辑"}},
    {id:1,enabled:true,label:{en:"Sources",zh:"来源"}},
    {id:2,enabled:true,label:{en:"Discuss",zh:"讨论"}},
    {id:3,enabled:true,label:{en:"Save selected",zh:"保存所选"}},
    {id:4,enabled:true,label:{en:"Discard proposal",zh:"放弃提案"}},
  ];
  const menu=mode==="confirm"?reviewActions:listActions;
  const activeAction=reconcileSelection(menu,actionIndex);
  useInput((input, key) => {
    if (mode === "harvest" || mode === "edit") {
      if (busy) return;
      if (key.escape) { cmdInput?.setValue(""); setMode(mode === "edit" ? "confirm" : "list"); return; }
      if (mode === "edit" && key.tab) {
        const revised = keepField();
        if (revised && cmdInput) {
          const next = (fieldIndex + 1) % EDIT_FIELDS.length;
          setFieldIndex(next); cmdInput.setValue(fieldValue(revised, EDIT_FIELDS[next]!));
        }
        return;
      }
      if (key.return && cmdInput) {
        if (mode === "edit") {
          if (keepField()) { cmdInput.setValue(""); setMode("confirm"); }
        } else if (cmdInput.value.trim()) {
          setBusy(true); setStatus("");
          void lessonHarvestPreview(client, project, cmdInput.value.trim())
            .then(value => { cmdInput.setValue(""); openProposal(value); })
            .catch(error => setStatus(String(error?.message || error))).finally(() => setBusy(false));
        }
        return;
      }
      if (cmdInput) {
        if (key.backspace) applyTextOp({op: "delete_back"}, cmdInput);
        else if (key.delete) applyTextOp({op: "delete_forward"}, cmdInput);
        else if (key.leftArrow) applyTextOp({op: "move_cursor", delta: -1}, cmdInput);
        else if (key.rightArrow) applyTextOp({op: "move_cursor", delta: 1}, cmdInput);
        else if (input && !key.ctrl && !key.meta) applyTextOp({op: "insert", text: input}, cmdInput);
      }
      return;
    }
    if (mode === "evidence") { if (key.escape) setMode("confirm"); return; }
    if (mode === "confirm" || mode === "list") {
      if(busy)return;
      const command=standardKey(input,key);
      if(command==="back"){
        if(mode==="confirm"){setMode("list");setProposal(null);setActionsFocused(true);setActionIndex(0);void refresh();}
        else onDismiss();
        return;
      }
      if(command==="next"){setActionsFocused(v=>!v);return;}
      if(!actionsFocused){
        if(command==="up"||command==="down"){
          const delta=command==="up"?-1:1;
          if(mode==="confirm")setCandidateIndex(v=>Math.max(0,Math.min(reviews.length-1,v+delta)));
          else setSel(v=>Math.max(0,Math.min((lessons?.pending?.length||1)-1,v+delta)));
          scrollRef.current?.scrollTo(0);
        }else if(input===" "&&mode==="confirm"&&!key.paste&&!key.ctrl&&!key.meta)
          setReviews(current=>current.map((item,index)=>index===candidateIndex?{...item,action:item.action==="accept"?"discard":"accept"}:item));
        else if(command==="activate")setActionsFocused(true);
        return;
      }
      if(command==="left"||command==="right"){setActionIndex(v=>moveSelection(menu,v,command==="left"?-1:1)??v);return;}
      if(command!=="activate"||activeAction===undefined)return;
      if(mode==="list"){
        if(activeAction===0)openProposal(proposals[0]);
        else if(activeAction===1){setMode("search");setQuery("");setSearchResults(null);}
        else if(activeAction===2&&cmdInput){cmdInput.setValue("");setMode("harvest");setStatus("");}
        else if(activeAction===3){
          const item=lessons?.pending?.[sel];
          if(item){setBusy(true);void lessonVerify(client,project,item.id,item.content_version)
            .then(()=>{setStatus(t({en:"Lesson reviewed.",zh:"已审阅经验。"}));void refresh();})
            .catch(error=>setStatus(String(error?.message||error))).finally(()=>setBusy(false));}
        }
      }else if(activeAction===0&&cmdInput){setFieldIndex(0);cmdInput.setValue(reviews[candidateIndex]?.trigger||"");setMode("edit");setStatus("");}
      else if(activeAction===1){
        setBusy(true);setStatus("");
        void lessonHarvestContext(client,project,proposalRef()).then(value=>{setEvidence(value);setMode("evidence");scrollRef.current?.scrollTo(0);})
          .catch(error=>setStatus(String(error?.message||error))).finally(()=>setBusy(false));
      }else if(activeAction===2){setSidecarId(crypto.randomUUID());setMode("discuss");}
      else if(activeAction===3)submit("accept");
      else if(activeAction===4)submit("discard");
      return;
    }
    for (const action of resolveLessonsKey(mode as "list" | "search", input, key)) {
      if (action.type === "dismiss") onDismiss();
      else if (action.type === "move") setSel(value => Math.max(0, Math.min((lessons?.pending?.length || 1) - 1, value + action.delta)));
      else if (action.type === "searchMode") { setMode("search"); setQuery(""); setSearchResults(null); }
      else if (action.type === "harvestMode" && cmdInput) { cmdInput.setValue(""); setMode("harvest"); setStatus(""); }
      else if (action.type === "verify") {
        const item = lessons?.pending?.[sel];
        if (item && !busy) {
          setBusy(true);
          void lessonVerify(client, project, item.id, item.content_version).then(() => { setStatus(t({en:"Lesson reviewed.",zh:"已审阅经验。"})); void refresh(); })
            .catch(error => setStatus(String(error?.message || error))).finally(() => setBusy(false));
        }
      } else if (action.type === "searchBack") { setMode("list"); setQuery(""); setSearchResults(null); }
      else if (action.type === "searchRun" && query.trim()) void lessonSearch(client, project, query.trim())
        .then((r: any) => setSearchResults(Array.isArray(r) ? r : r?.lessons || []))
        .catch(error => setStatus(String(error?.message || error)));
      else if (action.type === "searchBackspace") setQuery(value => value.slice(0, -1));
      else if (action.type === "searchInsert") setQuery(value => value + action.text);
    }
  }, {isActive: interactive && mode !== "discuss"});

  if (mode === "discuss" && proposal) return <BtwPanel client={client} project={project} processId="" cols={w}
    question={t({en:"Discuss this knowledge candidate: what do its sources actually support, what should change in its conditions, rule, validation or limitations?",zh:"讨论此知识候选：来源实际支持哪些内容？适用条件、规则、验证或限制应如何调整？"})}
    answer="" sidecarId={sidecarId} processes={{}} lessonProposal={{...proposalRef(), draft: {trigger: reviews[candidateIndex]!.trigger,
      rule: reviews[candidateIndex]!.rule, qualification: reviews[candidateIndex]!.qualification}}}
    onDismiss={() => setMode("confirm")} onApply={() => {}} />;
  const current = reviews[candidateIndex];
  const statusRow = status ? <Text color={colors.warning}>{status}</Text> : null;
  if (mode === "edit") return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Edit candidate ",zh:"编辑候选 "})}{candidateIndex + 1} · {t(FIELD_LABELS[EDIT_FIELDS[fieldIndex]!])}</Text>
    <Text dimColor>{t({en:"Use the NormalBar below. Draft changes are saved only after you submit the proposal.",zh:"在下方输入框编辑草稿，提交提案后才保存。"})}</Text>
    <Text dimColor>{t({en:"Tab moves through trigger, rule, type, conditions, validation, novelty and limitations.",zh:"Tab 切换触发条件、规则、类型、适用条件、验证、新颖性和限制。"})}</Text>{statusRow}
  </PageFrame>;
  if (mode === "harvest") return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Harvest a lesson · ",zh:"提炼经验 · "})}{project}</Text>
    <Text dimColor>{t({en:"Describe the situation, reusable action and evidence in the NormalBar.",zh:"在下方输入框描述情境、可复用的做法和证据。"})}</Text>
    {busy ? <Text dimColor>{t({en:"Analyzing…",zh:"正在分析…"})}</Text> : null}{statusRow}
  </PageFrame>;
  if (mode === "confirm" && current) return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Review knowledge proposal · ",zh:"审阅知识提案 · "})}{candidateIndex + 1}/{reviews.length}</Text>
    <Box gap={1}>{reviews.map((item, index) => {
      const style = useSelectionStyle(index === candidateIndex ? "focused" : "non-focused", "block");
      return <Text key={index} color={style.fg} backgroundColor={style.bg}> {item.action === "accept" ? "+" : "−"} {index + 1} </Text>;
    })}</Box>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      <Text bold>{current.trigger}</Text><Text>{current.rule}</Text>
      {proposal?.candidates?.[candidateIndex]?.evidence?.workspace_excerpts?.length
        ? <Text dimColor>{t({en:"Evidence covers selected passages. Sources shows the captured text; other dependencies may need review.",zh:"证据仅覆盖选定片段，可通过来源操作查看；其他依赖可能仍需核查。"})}</Text> : null}
      {Object.entries(current.qualification).map(([name, value]) => <Box key={name} flexDirection="column" marginTop={1}>
        <Text bold>{name in FIELD_LABELS ? t(FIELD_LABELS[name as keyof typeof FIELD_LABELS]) : name}</Text><Text dimColor>{value}</Text>
      </Box>)}
    </ScrollBox>
    {busy ? <Text dimColor>{t({en:"Working…",zh:"正在处理…"})}</Text> : null}{statusRow}
    <PageActions items={reviewActions} selected={activeAction} focused={interactive&&actionsFocused}/>
    <Text dimColor>{t({en:"↑/↓ candidate · Space include/exclude · Tab actions · ←/→ action · Enter open · Esc back",zh:"↑/↓ 候选 · 空格纳入/排除 · Tab 操作 · ←/→ 操作 · Enter 打开 · Esc 返回"})}</Text>
  </PageFrame>;
  if (mode === "evidence") return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Candidate sources · read-only excerpts",zh:"候选来源 · 只读摘录"})}</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {(evidence?.evidence_binding?.selected_excerpts || []).map((item: any, index: number) => <Box key={"passage-" + index} marginTop={1} flexDirection="column">
        <Text bold>{t({en:"Captured passage · ",zh:"捕获片段 · "})}{item.path}:{item.start_line}–{item.end_line}</Text>
        <Text>{item.text}</Text>
        <Text dimColor>{t({en:"Snapshot from the change; does not verify the rest of the file or its dependencies.",zh:"摘录来自变更快照，不代表已核验文件其余部分或依赖。"})}</Text>
      </Box>)}
      {evidence?.evidence_binding?.excerpts_partial ? <Text dimColor>{t({en:"Additional captured passages omitted from this view.",zh:"此视图省略了其他已捕获片段。"})}</Text> : null}
      {(evidence?.sources || []).map((source: any, index: number) => <Box key={index} marginTop={1} flexDirection="column">
        <Text bold>{t({en:"Source ",zh:"来源 "})}{index + 1} · {source.signal_type} · {source.created_at}</Text>
        <Text>{source.excerpt}</Text>{source.truncated ? <Text dimColor>{t({en:"Excerpt truncated.",zh:"摘录已截断。"})}</Text> : null}
      </Box>)}
      {evidence?.sources_partial ? <Text dimColor>{t({en:"More sources exist beyond this bounded view.",zh:"此视图范围外还有其他来源。"})}</Text> : null}
    </ScrollBox><Text dimColor>{t({en:"PgUp/PgDn scroll · Esc return to review",zh:"PgUp/PgDn 滚动 · Esc 返回审阅"})}</Text>
  </PageFrame>;
  if (mode === "search") return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Lesson search · ",zh:"搜索经验 · "})}{project}</Text><Text>{t({en:"query: ",zh:"查询："})}{query || "_"}</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {searchResults?.map((item: any, index: number) => <Text key={index}>{item.rule || item.trigger}</Text>)}
    </ScrollBox>{statusRow}<Text dimColor>{t({en:"Enter search · Esc back",zh:"Enter 搜索 · Esc 返回"})}</Text>
  </PageFrame>;
  if (loading) return <Box padding={1}><Text dimColor>{t({en:"Loading lessons…",zh:"正在读取经验…"})}</Text></Box>;
  const selected = lessons?.pending?.[sel];
  return <PageFrame page={pages.lessons} width={w}>
    <Text bold>{t({en:"Lessons · ",zh:"经验 · "})}{project}</Text>
    <Text color={proposals.length ? colors.accent : undefined}>{proposals.length} {t({en:"proposals awaiting review",zh:"项提案待审阅"})}</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {proposals.slice(0, 4).map(value => <Text key={value.proposal_id} dimColor>
        {value.created_at} · {value.candidates?.[0]?.trigger || t({en:"Knowledge candidate",zh:"知识候选"})}
      </Text>)}
      <LessonsTab lessons={lessons} width={w - 2} />
      {selected ? <Text>{selected.rule}</Text> : null}
    </ScrollBox>{statusRow}
    <PageActions items={listActions} selected={activeAction} focused={interactive&&actionsFocused}/>
    <Text dimColor>{t({en:"Tab list/actions · ←/→ action · ↑/↓ select · Enter open · Esc back",zh:"Tab 列表/操作 · ←/→ 操作 · ↑/↓ 选择 · Enter 打开 · Esc 返回"})}</Text>
  </PageFrame>;
});
