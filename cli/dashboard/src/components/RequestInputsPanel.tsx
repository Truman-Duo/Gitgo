import React, {useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {runtimeTrace} from "../backend/tools.js";
import type {TraceRecord} from "../traceView.js";
import {useManagedInput, useScrollInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {useCopy} from "../ui/PageFrame.js";
import {colors, usePanelSize} from "../theme/index.js";
import {SelectionRow, SelectionText} from "./Selection.js";
import {recordedInputSources} from "../chat/requestInputs.js";

const clean = (value: unknown) => String(value ?? "").replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, " ");
const record = (value: unknown): Record<string, any> => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const chunkSize = 12000;
const requestKey = (event: TraceRecord) => `${event.process_id ?? ""}/${event.seq}`;

/** Read the exact event/detail pair; current configuration and source files are never consulted. */
export function RequestInputsPanel({client, project, events, partial = false, onDismiss}: {
  client: BackendClient; project: string; events: TraceRecord[]; partial?: boolean; onDismiss: () => void;
}) {
  const t = useCopy(), {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const requests = useMemo(() => events.filter(event => ["provider_request_started", "provider_request_parameters"].includes(event.event)), [events]);
  const [selected, setSelected] = useState(""), [opened, setOpened] = useState<TraceRecord | null>(null);
  const scroll = useRef<ScrollBoxHandle>(null), selectedElement = useRef<DOMElement>(null);
  const at = Math.max(0, requests.findIndex(event => requestKey(event) === selected));
  const help = useLocalHelpCommand("request-inputs", undefined, !opened);
  useScrollInput(scroll, !opened);
  useLayoutEffect(() => {if (selectedElement.current) scroll.current?.scrollIntoView(selectedElement.current);}, [selected, w, h]);
  useManagedInput((text, key) => {
    if (help.handle(text, key)) return;
    if (matchChord("escape", text, key)) onDismiss();
    else if (matchChord("up", text, key)) setSelected(requests[Math.max(0, at - 1)] ? requestKey(requests[Math.max(0, at - 1)]!) : "");
    else if (matchChord("down", text, key)) setSelected(requests[Math.min(requests.length - 1, at + 1)] ? requestKey(requests[Math.min(requests.length - 1, at + 1)]!) : "");
    else if (matchChord("enter", text, key) && requests[at]) setOpened(requests[at]!);
  }, {isActive: !opened, clipboard: false});
  if (opened) return <RequestInputDetail key={`${project}/${requestKey(opened)}/${opened.detail_ref ?? ""}`} client={client} project={project}
    event={opened} onDismiss={() => setOpened(null)}/>;
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{t({en: "Recorded model inputs", zh: "已记录的模型输入"})}</Text>
    {partial ? <Text color={colors.warning}>{t({en: "This turn has more trace pages. Only loaded requests appear here.", zh: "此轮还有未读取的执行记录，此处仅列出已载入请求。"})}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {!requests.length ? <Text dimColor>{t({en: "No request inputs were recorded in this page.", zh: "当前页没有已记录的模型请求。"})}</Text> : null}
      {requests.map((event, index) => <Box key={requestKey(event)} ref={index === at ? selectedElement : undefined}>
        <SelectionRow selected={index === at}><SelectionText wrap="wrap">{clean(event.time)} · {clean(event.process_id)} · {clean(record(event.request_parameters).model ?? event.model ?? t({en: "Model unknown", zh: "模型未知"}))}</SelectionText></SelectionRow>
      </Box>)}
    </ScrollBox>
  </Box>;
}

function RequestInputDetail({client, project, event, onDismiss}: {client: BackendClient; project: string; event: TraceRecord; onDismiss: () => void}) {
  const t = useCopy(), {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [detail, setDetail] = useState<Record<string, any> | null>(null), [error, setError] = useState("");
  const [selected, setSelected] = useState(""), [opened, setOpened] = useState(""), [offset, setOffset] = useState(0);
  const scroll = useRef<ScrollBoxHandle>(null), selectedElement = useRef<DOMElement>(null);
  const help = useLocalHelpCommand("request-inputs");
  useEffect(() => {
    if (!event.detail_ref) return;
    let alive = true;
    void runtimeTrace(client, project, {action: "detail", ref: event.detail_ref})
      .then((value: any) => {if (alive) setDetail(record(value.detail));})
      .catch((failure: any) => {if (alive) setError(clean(failure?.message ?? failure));});
    return () => {alive = false;};
  }, [client, project, event.detail_ref]);
  const {provenance,exact,sources:recordedSources}=recordedInputSources(detail);
  const parameters = record(event.request_parameters);
  const unknown = t({en: "Not recorded", zh: "未记录"});
  const value = (object: Record<string, any>, key: string) => !Object.hasOwn(object, key) ? unknown
    : object[key] === null ? t({en: "Not sent", zh: "未发送"}) : clean(typeof object[key] === "object" ? JSON.stringify(object[key]) : object[key]);
  const inputs = useMemo(() => {
    const rows = recordedSources.map(({id,source,content}) => ({id, source,
      name: Array.isArray(source.sections) && source.sections.length ? source.sections.map((section: any) => clean(section?.name ?? unknown)).join(" · ") : clean(source.message_type ?? unknown),
      content}));
    if (Object.keys(record(parameters.configuration)).length) rows.push({id: "configuration", name: t({en: "Recorded parameter sources", zh: "已记录的参数来源"}), source: {}, content: parameters.configuration});
    if (detail && Array.isArray(detail.tools)) rows.push({id: "tools", name: t({en: "Tool declarations", zh: "工具声明"}), source: {}, content: detail.tools});
    return rows;
  }, [detail, event.request_parameters, t]);
  const active = inputs.find(row => row.id === opened), at = Math.max(0, inputs.findIndex(row => row.id === selected));
  const body = useMemo(() => active ? typeof active.content === "string" ? active.content : JSON.stringify(active.content, null, 2) ?? unknown : "", [active, unknown]);
  useLayoutEffect(() => {scroll.current?.scrollTo(0);}, [opened]);
  useLayoutEffect(() => {if (selectedElement.current) scroll.current?.scrollIntoView(selectedElement.current);}, [selected, opened, offset, w, h]);
  useScrollInput(scroll);
  useManagedInput((text, key) => {
    if (help.handle(text, key)) return;
    if (matchChord("escape", text, key)) {if (opened) {setOpened(""); setOffset(0);} else onDismiss(); return;}
    if (matchChord("up", text, key)) {
      if (opened) scroll.current?.scrollBy(-1); else setSelected(inputs[Math.max(0, at - 1)]?.id ?? "");
    } else if (matchChord("down", text, key)) {
      if (opened) scroll.current?.scrollBy(1); else setSelected(inputs[Math.min(inputs.length - 1, at + 1)]?.id ?? "");
    } else if (opened && matchChord("right", text, key)) {setOffset(value => Math.min(Math.max(0, Math.floor((body.length - 1) / chunkSize) * chunkSize), value + chunkSize)); scroll.current?.scrollTo(0);}
    else if (opened && matchChord("left", text, key)) {setOffset(value => Math.max(0, value - chunkSize)); scroll.current?.scrollTo(0);}
    else if (matchChord("enter", text, key) && inputs[at]) {setOpened(inputs[at]!.id); setOffset(0);}
  }, {clipboard: false});
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{t({en: "Request input snapshot", zh: "请求输入快照"})}</Text>
    <Text dimColor wrap="truncate-end">{clean(event.time)} · {clean(event.process_id)}</Text>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      <Text>{t({en: "Values selected for this request; server acceptance is unconfirmed.", zh: "此处为本次请求采用值，服务端是否接受仍未确认。"})}</Text>
      <Text>{t({en: "Model", zh: "模型"})}: {value(parameters, "model")} · {t({en: "Protocol", zh: "协议"})}: {value(parameters, "protocol")}</Text>
      <Text>{t({en: "Effort", zh: "思考强度"})}: {value(parameters, "effort")}</Text>
      <Text>{t({en: "Client context budget", zh: "客户端上下文预算"})}: {value(parameters, "context_window")} · {t({en: "Output limit", zh: "输出上限"})}: {value(parameters, "max_output_tokens")}</Text>
      {error ? <Text color={colors.danger}>{error}</Text> : event.detail_ref && detail === null ? <Text dimColor>{t({en: "Reading saved input…", zh: "正在读取已保存输入…"})}</Text> : null}
      {(!event.detail_ref || detail && !exact) ? <Text color={colors.warning}>{t({en: "An exact source snapshot was not recorded. Current files and settings cannot fill in this history.", zh: "未记录确切来源快照，不能用当前文件或设置补写这段历史。"})}</Text> : null}
      {exact ? <Text dimColor>{t({en: "Request body digest", zh: "请求体摘要"})}: {clean(provenance.wire_body_sha256 ?? unknown)}</Text> : null}
      {active ? <>
        <Text bold>{clean(active.name)}</Text>
        {(Array.isArray(active.source.sections) ? active.source.sections : []).map((section: any, index: number) => <Text key={index} dimColor>{clean(section.name)} · {clean(section.provenance ?? unknown)} · {t({en: "Version", zh: "版本"})}: {clean(section.version ?? unknown)}</Text>)}
        {active.source.content_sha256 ? <Text dimColor>{t({en: "Content digest", zh: "内容摘要"})}: {clean(active.source.content_sha256)}</Text> : null}
        {body.length > chunkSize ? <Text color={colors.warning}>{t({en: "This content is shown in bounded chunks; other content remains available.", zh: "内容按有界片段显示，其余内容仍可读取。"})}</Text> : null}
        <Text>{clean(body.slice(offset, offset + chunkSize))}</Text>
      </> : inputs.map((row, index) => <Box key={row.id} ref={index === at ? selectedElement : undefined}>
        <SelectionRow selected={index === at}><SelectionText wrap="wrap">{clean(row.name)}</SelectionText></SelectionRow>
      </Box>)}
    </ScrollBox>
  </Box>;
}
