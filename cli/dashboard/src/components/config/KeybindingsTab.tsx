import React, {memo, useCallback, useEffect, useRef, useState} from "react";
import {Box, ScrollBox, Text, type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput, useScrollInput} from "../../input/runtime.js";
import {INPUT_ACTIONS, InputBindingRegistry, presetSettings, type KeyboardPreset} from "../../input/router.js";
import {matchChord, chordLabel} from "../../input/bindings.js";
import {configGet, configSet} from "../../backend/tools.js";
import {notifyGeneralConfigChanged} from "../../hooks/useGeneralConfig.js";
import {useCopy} from "../../ui/PageFrame.js";
import type {Copy} from "../../ui/pageContract.js";
import {colors, usePanelSize} from "../../theme/index.js";
import type {ConfigTabProps} from "./types.js";

const LABELS: Record<string, Copy> = {
  "execution.interrupt": {en:"Interrupt / return",zh:"中断 / 返回"},
  "navigation.confirm": {en:"Confirm / send",zh:"确认 / 发送"},
  "editor.newline": {en:"New line",zh:"输入换行"},
  "navigation.tab": {en:"Next / focus",zh:"下一项 / 聚焦"},
  "navigation.previousTab": {en:"Previous / focus",zh:"上一项 / 聚焦"},
  "navigation.up": {en:"Up",zh:"向上"},
  "navigation.down": {en:"Down",zh:"向下"},
  "navigation.left": {en:"Left",zh:"向左"},
  "navigation.right": {en:"Right",zh:"向右"},
  "viewport.pageUp": {en:"Page up",zh:"向上翻页"},
  "viewport.pageDown": {en:"Page down",zh:"向下翻页"},
  "clipboard.copy": {en:"Copy selection",zh:"复制所选文字"},
  "clipboard.paste": {en:"Paste text",zh:"粘贴文字"},
  "editor.backspace": {en:"Delete before cursor",zh:"删除前一字符"},
  "editor.delete": {en:"Delete after cursor",zh:"删除后一字符"},
  "editor.start": {en:"Start of draft",zh:"草稿开头"},
  "editor.end": {en:"End of draft",zh:"草稿末尾"},
  "editor.previousWord": {en:"Previous word",zh:"前一词"},
  "editor.nextWord": {en:"Next word",zh:"后一词"},
  "editor.killToEnd": {en:"Cut to end",zh:"剪切到末尾"},
  "editor.killToStart": {en:"Cut to start",zh:"剪切到开头"},
  "editor.killWord": {en:"Cut previous word",zh:"剪切前一词"},
  "editor.yank": {en:"Paste cut text",zh:"粘贴剪切文字"},
  "editor.external": {en:"External editor",zh:"外部编辑器"},
};

export const KeybindingsTab = memo(function KeybindingsTab({
  client, report, onFooter, onStatusUpdate, shell, contentFocused,
}: ConfigTabProps) {
  const t = useCopy(), {w} = usePanelSize({widthOffset: 6});
  const [preset, setPreset] = useState<KeyboardPreset>("standard");
  const [draft, setDraft] = useState<KeyboardPreset>("standard");
  const [loaded, setLoaded] = useState(false), [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false), [row, setRow] = useState(0);
  const [message, setMessage] = useState("");
  const scroll = useRef<ScrollBoxHandle>(null);
  useScrollInput(scroll, contentFocused && row >= 2);
  const label = (value: KeyboardPreset) => value === "portable"
    ? t({en:"Portable",zh:"兼容方案"}) : t({en:"Standard",zh:"标准方案"});
  const registry = new InputBindingRegistry();
  registry.configure(presetSettings(preset));
  useEffect(() => {
    let active = true;
    configGet(client).then((value: any) => {
      if (!active) return;
      const selected = value?.keyboard_preset === "portable" ? "portable" : "standard";
      setPreset(selected); setDraft(selected); setLoaded(true);
    }).catch(() => {if(active)setMessage(t({en:"Could not load bindings. Reopen to retry.",zh:"无法读取绑键，请重新打开重试。"}));});
    return () => {active = false;};
  }, [client]);
  useEffect(() => {report({sub: editing, fullscreen: false});}, [editing, report]);
  useEffect(() => {onFooter({hidden:true}); return () => onFooter(null);}, [onFooter]);
  useEffect(() => {onStatusUpdate?.(message);}, [message, onStatusUpdate]);
  const save = useCallback(async (selected: KeyboardPreset) => {
    if (!loaded || saving) return;
    setSaving(true);
    try {
      await configSet(client, "keyboard_preset", selected);
      setPreset(selected); setDraft(selected); setEditing(false);
      notifyGeneralConfigChanged();
      setMessage(t({en:"Saved",zh:"已保存"}));
    } catch {
      setMessage(t({en:"Save failed. Current bindings remain active.",zh:"保存失败，继续使用当前绑键。"}));
    } finally {setSaving(false);}
  }, [client, loaded, saving, t]);
  useInput((input, key) => {
    if (!contentFocused) return false;
    if (saving) return;
    if (matchChord("escape", input, key)) {
      if (editing) {setEditing(false); setDraft(preset);}
      else shell.leaveContent();
      return;
    }
    if (!loaded || key.paste) return;
    if (editing) {
      if (matchChord("left", input, key) || matchChord("right", input, key))
        setDraft(value => value === "standard" ? "portable" : "standard");
      else if (matchChord("enterNoShift", input, key)) void save(draft);
      return;
    }
    if (matchChord("up", input, key)) {if(row > 2) scroll.current?.scrollBy(-1); setRow(value => Math.max(0, value-1));}
    else if (matchChord("down", input, key)) {if(row >= 2) scroll.current?.scrollBy(1); setRow(value => Math.min(Object.keys(INPUT_ACTIONS).length+1, value+1));}
    else if (matchChord("enterNoShift", input, key)) {
      if (row === 0) {setDraft(preset); setEditing(true);}
      else if (row === 1) void save("standard");
    }
  });
  return <Box flexDirection="column" flexGrow={1} minHeight={0}>
    <Text bold={contentFocused && row === 0} color={contentFocused && row === 0 ? colors.selection.row.fg : undefined}>
      {t({en:"Binding scheme",zh:"绑键方案"})}: {loaded ? label(editing ? draft : preset) : t({en:"Loading…",zh:"读取中…"})}
    </Text>
    <Text bold={contentFocused && row === 1} color={contentFocused && row === 1 ? colors.selection.row.fg : undefined}>
      {t({en:"Restore defaults",zh:"恢复默认"})}
    </Text>
    <Text dimColor>{editing
      ? chordLabel("leftRight")+" "+t({en:"choose",zh:"选择"})+" · "+chordLabel("enter")+" "+t({en:"save",zh:"保存"})+" · "+chordLabel("escape")+" "+t({en:"cancel",zh:"取消"})
      : t({en:"Portable uses Alt+Enter for new lines and Ctrl+P for previous focus.",zh:"兼容方案使用 Alt+Enter 换行、Ctrl+P 向前切换焦点。"})}</Text>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
      {Object.keys(INPUT_ACTIONS).map((action, index) => <Box key={action} flexDirection="row" flexShrink={0}>
        <Box width={Math.max(8, w-18)}><Text wrap="truncate-end" bold={contentFocused && row === index+2}>{t(LABELS[action]!)}</Text></Box>
        <Text>{registry.label(action)}</Text>
      </Box>)}
    </ScrollBox>
    <Text dimColor>{t({en:"Bindings apply immediately and survive restart. The active view determines each action.",zh:"保存后立即生效，重启保留；动作含义随当前界面而定。"})}</Text>
  </Box>;
});
