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
type Mode = "list" | "search" | "harvest" | "confirm" | "edit" | "evidence" | "discuss";

export const LessonsPanel = memo(function LessonsPanel({client, project, initialQuery, onDismiss,
  interactive = true, cmdInput, onFooter}: Props) {
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
      statusText: mode === "edit" ? "Editing " + EDIT_FIELDS[fieldIndex] + " · Tab next field · Enter keep draft · Esc cancel"
        : "Enter analyze · Esc cancel"});
    return () => onFooter(null);
  }, [mode, fieldIndex, cmdInput, cmdInput?.value, cmdInput?.cursor, onFooter]);
  useEffect(() => {
    if (initialQuery?.trim()) void lessonSearch(client, project, initialQuery.trim())
      .then((r: any) => setSearchResults(Array.isArray(r) ? r : r?.lessons || []))
      .catch(error => setStatus(String(error?.message || error)));
  }, [initialQuery, client, project]);
  const openProposal = (value: any) => {
    setProposal(value); setReviews(proposalReviews(value)); setCandidateIndex(0);
    setMode("confirm"); setStatus(""); scrollRef.current?.scrollTo(0);
  };
  const proposalRef = () => ({proposal_id: proposal.proposal_id,
    proposal_version: proposal.proposal_version, candidate_index: candidateIndex});
  const submit = (action: "accept" | "discard") => {
    if (busy || !proposal) return;
    setBusy(true); setStatus("");
    void lessonHarvestResolve(client, project, proposal.proposal_id, action, proposal.proposal_version,
      action === "accept" ? reviewsForSubmit(reviews) : undefined).then((value: any) => {
      setStatus(value.status === "accepted" ? "Saved " + value.count + " reviewed lesson(s)." : "Proposal discarded.");
      setMode("list"); setProposal(null); void refresh();
    }).catch(error => setStatus(String(error?.message || error) + " · Return to the list and reopen if the proposal changed."))
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
    if (mode === "confirm") {
      if (busy) return;
      if (key.escape) { setMode("list"); setProposal(null); void refresh(); }
      else if (key.upArrow || key.downArrow) {
        setCandidateIndex(current => Math.max(0, Math.min(reviews.length - 1, current + (key.upArrow ? -1 : 1))));
        scrollRef.current?.scrollTo(0);
      } else if (input === " ") setReviews(current => current.map((item, index) => index === candidateIndex
        ? {...item, action: item.action === "accept" ? "discard" : "accept"} : item));
      else if (input.toLowerCase() === "e" && cmdInput) {
        setFieldIndex(0); cmdInput.setValue(reviews[candidateIndex]?.trigger || ""); setMode("edit"); setStatus("");
      } else if (input.toLowerCase() === "s") submit("accept");
      else if (input.toLowerCase() === "d") submit("discard");
      else if (input.toLowerCase() === "b") { setSidecarId(crypto.randomUUID()); setMode("discuss"); }
      else if (input.toLowerCase() === "f") {
        setBusy(true); setStatus("");
        void lessonHarvestContext(client, project, proposalRef()).then(value => {
          setEvidence(value); setMode("evidence"); scrollRef.current?.scrollTo(0);
        }).catch(error => setStatus(String(error?.message || error))).finally(() => setBusy(false));
      }
      return;
    }
    if (mode === "list" && input.toLowerCase() === "p" && !key.ctrl && !key.meta) {
      if (proposals[0]) openProposal(proposals[0]);
      else { setStatus("No proposal is waiting for review."); void refresh(); }
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
          void lessonVerify(client, project, item.id, item.content_version).then(() => { setStatus("Lesson reviewed."); void refresh(); })
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

  if (mode === "discuss" && proposal) return <BtwPanel client={client} project={project} processId=""
    question="Discuss this knowledge candidate: what do its sources actually support, what should change in its conditions, rule, validation or limitations?"
    answer="" sidecarId={sidecarId} processes={{}} lessonProposal={{...proposalRef(), draft: {trigger: reviews[candidateIndex]!.trigger,
      rule: reviews[candidateIndex]!.rule, qualification: reviews[candidateIndex]!.qualification}}}
    onDismiss={() => setMode("confirm")} onApply={() => {}} />;
  const current = reviews[candidateIndex];
  const statusRow = status ? <Text color={colors.warning}>{status}</Text> : null;
  if (mode === "edit") return <Box flexDirection="column" paddingX={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>Edit candidate {candidateIndex + 1} · {EDIT_FIELDS[fieldIndex]}</Text>
    <Text dimColor>Use the NormalBar below. Draft changes are saved only after you submit the proposal.</Text>
    <Text dimColor>Tab moves through trigger, rule, type, conditions, validation, novelty and limitations.</Text>{statusRow}
  </Box>;
  if (mode === "harvest") return <Box flexDirection="column" padding={1} width={w}>
    <Text bold>Harvest a lesson · {project}</Text>
    <Text dimColor>Describe the situation, reusable action and evidence in the NormalBar.</Text>
    {busy ? <Text dimColor>Analyzing…</Text> : null}{statusRow}
  </Box>;
  if (mode === "confirm" && current) return <Box flexDirection="column" paddingX={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>Review knowledge proposal · {candidateIndex + 1}/{reviews.length}</Text>
    <Box gap={1}>{reviews.map((item, index) => {
      const style = useSelectionStyle(index === candidateIndex ? "focused" : "non-focused", "block");
      return <Text key={index} color={style.fg} backgroundColor={style.bg}> {item.action === "accept" ? "+" : "−"} {index + 1} </Text>;
    })}</Box>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      <Text bold>{current.trigger}</Text><Text>{current.rule}</Text>
      {proposal?.candidates?.[candidateIndex]?.evidence?.workspace_excerpts?.length
        ? <Text dimColor>Evidence covers selected passages. F shows the captured text; other dependencies may need review.</Text> : null}
      {Object.entries(current.qualification).map(([name, value]) => <Box key={name} flexDirection="column" marginTop={1}>
        <Text bold>{name}</Text><Text dimColor>{value}</Text>
      </Box>)}
    </ScrollBox>
    {busy ? <Text dimColor>Working…</Text> : null}{statusRow}
    <Text dimColor>↑/↓ candidate · Space include/exclude · E edit · F sources · B discuss</Text>
    <Text dimColor>S save selected · D discard proposal · PgUp/PgDn scroll · Esc back</Text>
  </Box>;
  if (mode === "evidence") return <Box flexDirection="column" paddingX={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>Candidate sources · read-only excerpts</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {(evidence?.evidence_binding?.selected_excerpts || []).map((item: any, index: number) => <Box key={"passage-" + index} marginTop={1} flexDirection="column">
        <Text bold>Captured passage · {item.path}:{item.start_line}–{item.end_line}</Text>
        <Text>{item.text}</Text>
        <Text dimColor>Snapshot from the change; does not verify the rest of the file or its dependencies.</Text>
      </Box>)}
      {evidence?.evidence_binding?.excerpts_partial ? <Text dimColor>Additional captured passages omitted from this view.</Text> : null}
      {(evidence?.sources || []).map((source: any, index: number) => <Box key={index} marginTop={1} flexDirection="column">
        <Text bold>Source {index + 1} · {source.signal_type} · {source.created_at}</Text>
        <Text>{source.excerpt}</Text>{source.truncated ? <Text dimColor>Excerpt truncated.</Text> : null}
      </Box>)}
      {evidence?.sources_partial ? <Text dimColor>More sources exist beyond this bounded view.</Text> : null}
    </ScrollBox><Text dimColor>PgUp/PgDn scroll · Esc return to review</Text>
  </Box>;
  if (mode === "search") return <Box flexDirection="column" paddingX={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>Lesson search · {project}</Text><Text>query: {query || "_"}</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {searchResults?.map((item: any, index: number) => <Text key={index}>{item.rule || item.trigger}</Text>)}
    </ScrollBox>{statusRow}<Text dimColor>Enter search · Esc back</Text>
  </Box>;
  if (loading) return <Box padding={1}><Text dimColor>Loading lessons…</Text></Box>;
  const selected = lessons?.pending?.[sel];
  return <Box flexDirection="column" paddingX={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>Lessons · {project}</Text>
    <Text color={proposals.length ? colors.accent : undefined}>{proposals.length} proposal(s) awaiting review · P open next</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {proposals.slice(0, 4).map(value => <Text key={value.proposal_id} dimColor>
        {value.created_at} · {value.candidates?.[0]?.trigger || "Knowledge candidate"}
      </Text>)}
      <LessonsTab lessons={lessons} width={w - 2} />
      {selected ? <Text>{selected.rule}</Text> : null}
    </ScrollBox>{statusRow}
    <Text dimColor>P proposals · S search · H harvest · V review legacy · ↑/↓ select · Esc back</Text>
  </Box>;
});
