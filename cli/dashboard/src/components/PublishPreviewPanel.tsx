import React, {useEffect, useMemo, useRef, useState} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import type {FooterConfig} from "./CommandBar.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {parseUnifiedDiff} from "../utils/diff.js";
import {DiffView} from "./DiffView.js";
import {usePanelSize} from "../theme/index.js";

export function PublishPreviewPanel({client, project, itemId, binding, onFooter, onDismiss}: {
  client: BackendClient; project: string; itemId: string; binding: string;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
}) {
  const t = useCopy(), {w} = usePanelSize({minWidth: 20, widthOffset: 4}), scroll = useRef<ScrollBoxHandle>(null);
  const [result, setResult] = useState<{text: string; truncated: boolean; base_oid: string; tree_oid: string} | null>(null), [error, setError] = useState("");
  useEffect(() => {
    let active = true; onFooter({hidden: true});
    void client.callTool("publish.preview_diff", {project, item_id: itemId, binding}).then(value => {if (active) setResult(value as any);}).catch(e => {if (active) setError(String(e?.message || e));});
    return () => {active = false; onFooter(null);};
  }, [client, project, itemId, binding, onFooter]);
  const files = useMemo(() => parseUnifiedDiff((result?.text ?? "").replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, " ")), [result]);
  useManagedInput((text, key) => {
    if (matchChord("escape", text, key)) onDismiss();
    else if (matchChord("up", text, key)) scroll.current?.scrollBy(-1);
    else if (matchChord("down", text, key)) scroll.current?.scrollBy(1);
  });
  return <Box flexDirection="column" flexGrow={1} minHeight={0} paddingX={1}>
    <Text bold>{t({en: "Prepared combined changes", zh: "已准备的组合变更"})}</Text>
    {result ? <Text dimColor wrap="truncate-end">{result.base_oid.slice(0, 12)} → {result.tree_oid.slice(0, 12)}</Text> : null}
    {error ? <Text>{error}</Text> : !result ? <Text>{t({en: "Reading fixed content…", zh: "正在读取固定内容…"})}</Text> : null}
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}><DiffView files={files} width={w}/>
      {result?.truncated ? <Text>{t({en: "The patch reached its display limit; omitted changes are not approved by this view.", zh: "差异达到显示上限，不能据此认为省略部分已核对。"})}</Text> : null}</ScrollBox>
    <Text dimColor>{t({en: "↑/↓ scroll · Esc back", zh: "↑/↓ 滚动 · Esc 返回"})}</Text>
  </Box>;
}
