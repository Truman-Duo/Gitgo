import {SelectionText} from "./Selection.js";
import React, {useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, TerminalRaster, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import type {PublishItem} from "../backend/publish.js";
import type {FooterConfig} from "./CommandBar.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {colors, truncate, usePanelSize} from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";
import {useGraphicsCapability} from "../ui/terminalGraphics.js";
import {roleGraphSvg} from "./publishGraphSvg.js";
import {useSvgRaster} from "../ui/svgRaster.js";
import {publishCommands, publishSuggestions} from "./publishCommands.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {resolveCommandKeys, applyCommandAction} from "../input/commandInput.js";

const clean = (value: string) => value.replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
/** Business-role relationships only. Git parent edges belong to PublishHistoryPanel. */
export function publishRoleFlow(item: PublishItem) {
  const targetRole = item.plan?.destination === "formal" ? "formal" : item.plan ? "workspace" : null;
  const sources = item.plan?.contributions?.length ? item.plan.contributions : item.candidate ? [item.candidate] : [];
  return {targetRole, edges: targetRole ? sources.map(source => ({from: source.role, to: targetRole,
    sourceRef: source.source_ref, oid: source.oid, targetRef: item.plan!.target_ref})) : []};
}

export function PublishFlowPanel({item, onFooter, onDismiss, onOpenNode, onCommand, localState}: {
  item: PublishItem; onFooter: (footer: FooterConfig | null) => void; onDismiss: () => void;
  onOpenNode: (index: number) => void; localState: PublishLocalState;
  onCommand: (command: string) => void;
}) {
  const t = useCopy(), {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const commands = publishCommands({item}).filter(row => !["flow", "settings", "refresh", "more", "prepare", "formal", "combine", "account", "share", "share-refresh", "send", "platform-reconcile"].includes(row.name));
  for (const draft of item.collaboration?.drafts ?? []) {
    if (draft.state === "draft" && item.allowed_actions.includes("collaborate_send")) commands.push({name: `send ${draft.id}`, title: {en: `Send draft ${draft.id}`, zh: `发送草稿 ${draft.id}`}});
    if (["sending", "unknown"].includes(draft.state) && item.allowed_actions.includes("collaborate_reconcile")) commands.push({name: `platform-reconcile ${draft.id}`, title: {en: `Reconcile draft ${draft.id}`, zh: `核对草稿 ${draft.id}`}});
  }
  const runSteps = item.plan?.destination === "formal" ? "verify execute" : "adopt";
  const rows = [...item.nodes.map(node => ({id: node.id, node})), ...commands.map(command => ({id: "action:" + command.name, command}))];
  const [selected, setSelected] = useState(() => Math.max(0, rows.findIndex(node =>
    node.id === localState.get<string>(`flow/${item.id}`))));
  const index = Math.min(selected, Math.max(0, rows.length - 1));
  const input = useTextInput(), [suggestionIdx, setSuggestionIdx] = useState(0);
  const suggestions = publishSuggestions({item}, input.value, t);
  const activeSuggestion = Math.min(suggestionIdx, Math.max(0, suggestions.length - 1));
  const flow = publishRoleFlow(item);
  const graphRows = h < 28 ? 3 : h < 40 ? 5 : 7;
  const terminalGraphics = useGraphicsCapability();
  const scene = useMemo(() => terminalGraphics.sixel ? roleGraphSvg(flow.edges, w, graphRows, terminalGraphics) : null,
    [item.id, item.revision, w, graphRows, terminalGraphics]);
  const {image: graphic, error: graphicsError} = useSvgRaster(scene);
  const scroll = useRef<ScrollBoxHandle>(null), selectedRow = useRef<DOMElement>(null);
  useLayoutEffect(() => {
    if (selectedRow.current) scroll.current?.scrollIntoView(selectedRow.current);
  }, [index, w, h, !!graphic]);
  useEffect(() => {onFooter(input.value.startsWith("/") ? {kind: "command", cmdInput: input, suggestions, suggestionIdx: activeSuggestion, cmdResult: "",
    statusText: t({en: "↑/↓ step or action · Enter open · Esc return", zh: "↑/↓ 步骤或操作 · Enter 进入 · Esc 返回"})} : {hidden: true});
    return () => onFooter(null);}, [onFooter, input, input.value, input.cursor, suggestionIdx, item]);
  useEffect(() => setSuggestionIdx(0), [input.value, item.revision]);
  useEffect(() => { localState.set(`flow/${item.id}`, rows[index]?.id); }, [item.id, index, localState]);
  useInput((text, key) => {
    if (input.value.startsWith("/")) {
      if (matchChord("escape", text, key)) {input.setValue(""); return;}
      for (const action of resolveCommandKeys(input.value, suggestions.length, text, key, suggestions[activeSuggestion]?.label ?? ""))
        applyCommandAction(action, {cmdInput: input, suggestionLabels: suggestions.map(row => row.label), suggestionIdx: activeSuggestion, setSuggestionIdx,
          runCommand: onCommand});
      return;
    }
    if (text === "/") {input.setValue("/"); return;}
    if (matchChord("escape", text, key)) onDismiss();
    const delta = matchChord("up", text, key) ? -1 : matchChord("down", text, key) ? 1 : 0;
    if (delta) setSelected(value => Math.max(0, Math.min(rows.length - 1, value + delta)));
    if (matchChord("home", text, key)) setSelected(0);
    if (matchChord("end", text, key)) setSelected(rows.length - 1);
    if (matchChord("enter", text, key) && rows[index]) {
      const row = rows[index]!;
      if ("node" in row) onOpenNode(index);
      else onCommand(row.command.name === "run" ? "/run " + runSteps : "/" + row.command.name);
    }
  });
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Box flexDirection="column" flexShrink={0}>
    <Text bold>{t(graphic ? {en: "Role flow", zh: "三角色流转"} : {en: "Role flow · compatibility view", zh: "三角色流转 · 兼容视图"})}</Text>
    <Text wrap="truncate-end">{clean(item.title)} · {item.status}</Text>
    {item.plan?.target_role === "workspace" ? <Text dimColor>{t({en: "Formal commits stay in a local Workspace branch.", zh: "正式提交保存在本地 Workspace 分支。"})}</Text> : null}
    {graphicsError ? <Text>{graphicsError}</Text> : null}
    {graphic ? <><TerminalRaster image={graphic} columns={w} rows={graphRows}/>
      <Box><Box width="33%" justifyContent="center"><Text>{t({en: "Workspace", zh: "工作区"})}</Text></Box><Box width="34%" justifyContent="center"><Text>{t({en: "Trial", zh: "试验"})}</Text></Box><Box width="33%" justifyContent="center"><Text>{t({en: "Formal", zh: "正式"})}</Text></Box></Box>
    </> : null}
    <Text dimColor wrap="truncate-end">{t({en: "Saved plan · Transfers are not yet complete", zh: "已保存计划 · 连线不表示传输已完成"})} · {clean(item.plan?.method ?? "—")}</Text>
    {flow.edges[0] ? <Text wrap="truncate-end">{clean(flow.edges[0].from.toUpperCase())} · {truncate(clean(flow.edges[0].sourceRef), Math.max(6, Math.floor(w / 3)))} @ {flow.edges[0].oid.slice(0, 8)} → {clean(flow.edges[0].to.toUpperCase())} · {clean(flow.edges[0].targetRef)}{flow.edges.length > 1 ? ` (+${flow.edges.length - 1})` : ""}</Text>
      : <Text wrap="truncate-end">{t({en: "No bound role transition. Prepare a candidate first.", zh: "尚未绑定角色流转，请先准备候选。"})}</Text>}
    </Box>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {rows.map((row, rowIndex) => <Box key={row.id} flexShrink={0} ref={rowIndex === index ? selectedRow : undefined}><SelectionText selected={rowIndex === index} wrap="truncate-end"

         >
        {"node" in row ? `${row.node.id} · ${row.node.status}${item.current_node === row.node.id ? " ◀" : ""}`
          : row.command.name === "run" ? t(item.plan?.destination === "formal" ? {en: "Start: verify → execute", zh: "开始处理：检查 → 执行计划"}
            : {en: "Start: adopt for rework", zh: "开始处理：接手继续完善"}) : t(row.command.title)}
      </SelectionText></Box>)}
      {flow.edges.slice(1).map((edge, edgeIndex) => <Text key={edgeIndex} wrap="truncate-end">{clean(edge.from)} · {clean(edge.sourceRef)} @ {edge.oid.slice(0, 12)} → {clean(edge.to)} · {clean(edge.targetRef)}</Text>)}
      <Text wrap="truncate-end">{t({en: "Owner", zh: "负责人"})}: {clean(item.owner ?? "—")}</Text>
      {item.rework ? <Text>{t({en: "Remaining work", zh: "剩余工作"})}: {clean(item.rework)}</Text> : null}
    </ScrollBox>
    {item.nodes[index] ? <Box flexShrink={0}><Text dimColor wrap="truncate-end">{clean(item.nodes[index]!.reason)}</Text></Box> : null}
  </Box>;
}
