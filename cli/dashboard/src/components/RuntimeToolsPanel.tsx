import {label} from "../ui/runtimeLabels.js";
import {useCopy} from "../ui/PageFrame.js";
import React, {memo, useCallback, useEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {customToolArchive, customToolList, customToolRestore} from "../backend/tools.js";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import {colors, truncate, usePanelSize, useSelectionStyle} from "../theme/index.js";

type Props = {client: BackendClient; project: string; cols: number; onDismiss: () => void; interactive?: boolean};

export const RuntimeToolsPanel = memo(function RuntimeToolsPanel({client, project, onDismiss, interactive = true}: Props) {
  const t=useCopy();
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, interactive);
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState(0);
  const [detail, setDetail] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const alive = useRef(true);
  const generation = useRef(0);
  const mutation = useRef(false);
  const {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const refresh = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    try {
      const result: any = await customToolList(client, project, true);
      if (!alive.current || request !== generation.current) return;
      setItems(Array.isArray(result?.tools) ? result.tools : []);
      setSelected(value => Math.max(0, Math.min(value, (result?.tools?.length || 1) - 1)));
    } catch (error: any) { if (alive.current && request === generation.current) setStatus(String(error?.message || error)); }
    finally { if (alive.current && request === generation.current) setLoading(false); }
  }, [client, project]);
  useEffect(() => {
    alive.current = true; setItems([]); setSelected(0); setDetail(false); setConfirm(false); setStatus("");
    void refresh();
    return () => {alive.current = false; generation.current++;};
  }, [refresh]);
  useEffect(() => {scrollRef.current?.scrollTo(0);}, [detail, selected]);
  useInput((input, key) => {
    if (key.escape) {
      if (confirm) setConfirm(false);
      else if (detail) setDetail(false);
      else onDismiss();
      return;
    }
    if (key.ctrl || key.meta || key.paste || loading || busy || !interactive) return;
    if (confirm) {
      if (key.return && items[selected] && !mutation.current) {
        const item = items[selected]; mutation.current = true; setBusy(true); setConfirm(false);
        const change = item.state === "archived" ? customToolRestore : customToolArchive;
        void change(client, project, item.name).then(async () => {
          if (alive.current) {setStatus(item.state === "archived" ? t({en:"Action restored",zh:"已恢复动作"}) : t({en:"Action archived",zh:"已归档动作"})); await refresh();}
        }).catch(error => {if (alive.current) setStatus(String(error));})
          .finally(() => {mutation.current = false; if (alive.current) setBusy(false);});
      }
      return;
    }
    if (detail) {if (key.return && items[selected]) setConfirm(true); return;}
    if (key.upArrow) setSelected(value => Math.max(0, value - 1));
    else if (key.downArrow) setSelected(value => Math.min(items.length - 1, value + 1));
    else if (key.return && items[selected]) setDetail(true);
  }, {isActive: interactive});
  const selection = useSelectionStyle(interactive ? "focused" : "non-focused", "row");
  const item = items[selected];
  return <Box flexDirection="column" paddingX={1} paddingTop={1} width={w} flexGrow={1} minHeight={0}>
    <Text bold>{t({en:"Saved custom actions —",zh:"已保存的自定义动作 —"})} {project}</Text>
    <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1} minHeight={0}>
      {loading ? <Text dimColor>{t({en:"Loading saved actions…",zh:"正在读取自定义动作…"})}</Text> : null}
      {!loading && !items.length ? <Text dimColor>{t({en:"No saved custom actions.",zh:"尚无已保存的自定义动作。"})}</Text> : null}
      {detail && item ? <>
        <Text bold>{item.name} · {t({en:"version",zh:"版本"})} {item.version}</Text>
        <Text>{item.description || t({en:"Description not recorded",zh:"未记录说明"})}</Text>
        <Text>{t({en:"State: ",zh:"状态："})}{label(item.state,t)} · {item.effect || t({en:"Effect not recorded",zh:"未记录效果"})}</Text>
        <Text>{t({en:"Source: project saved action · ",zh:"来源：项目自定义动作 · "})}{item.execution_mode || t({en:"Execution mode not recorded",zh:"未记录执行模式"})}</Text>
        <Text>{t({en:"Authority: ",zh:"权限："})}{item.authority === "user_approved_privileged" ? t({en:"Exact approval required per invocation",zh:"每次调用均需精确授权"}) : t({en:"See registered execution contract",zh:"查看已注册执行契约"})}</Text>
        <Text>{t({en:"Inputs: ",zh:"输入："})}{Object.entries(item.input_schema?.properties || {}).map(([name, spec]: [string, any]) => name + " (" + (spec?.type || t({en:"unspecified",zh:"未指定"})) + ")").join(", ") || t({en:"No declared fields",zh:"无声明字段"})}</Text>
        <Text>{t({en:"Required inputs: ",zh:"必填输入："})}{(item.input_schema?.required || []).join(", ") || t({en:"None declared",zh:"未声明"})}</Text>
        <Text>{t({en:"Output: ",zh:"输出："})}{item.output_schema?.type || t({en:"No separate output schema recorded",zh:"未单独记录输出结构"})}</Text>
        <Text>{t({en:"Resources: ",zh:"资源："})}{(item.resources || []).map((value: any) => typeof value === "string" ? value : value.path || value.kind || t({en:"Registered scope",zh:"已注册范围"})).join(", ") || t({en:"See execution contract",zh:"查看执行契约"})}</Text>
        <Text dimColor>{t({en:"Updated: ",zh:"更新时间："})}{item.updated_at || t({en:"Not recorded",zh:"未记录"})} · {item.version_count} {t({en:"saved versions",zh:"个已存版本"})}</Text>
        <Text dimColor>{t({en:"Digest: ",zh:"摘要："})}{item.digest || t({en:"Not recorded",zh:"未记录"})}</Text>
        <Text dimColor>{t({en:"Viewing does not mount, authorize or execute this action.",zh:"查看不会挂载、授权或执行此动作。"})}</Text>
      </> : items.map((value, index) => <Box key={value.tool_id || value.name} flexDirection="column">
        <Text color={index === selected ? selection.fg : undefined} backgroundColor={index === selected ? selection.bg : undefined}>
          {index === selected ? "› " : "  "}{truncate(value.name + "  v" + value.version + "  " + label(value.state,t), Math.max(10, w - 4))}
        </Text>
        {index === selected ? <Text dimColor>  {truncate(value.description || t({en:"No description",zh:"无说明"}), Math.max(10, w - 4))}</Text> : null}
      </Box>)}
    </ScrollBox>
    {status ? <Text color={colors.warning}>{status}</Text> : null}
    <Text dimColor>{confirm ? t({en:"Enter confirm · Esc cancel",zh:"Enter 确认 · Esc 取消"})
      : detail ? t({en:"Enter review archive/restore · Esc list",zh:"Enter 确认归档/恢复 · Esc 列表"})
      : t({en:"↑/↓ select · Enter details · Esc tabs",zh:"↑/↓ 选择 · Enter 详情 · Esc 页签"})}</Text>
  </Box>;
});
