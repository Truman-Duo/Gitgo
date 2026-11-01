import {SelectionText} from "../Selection.js";
import {useCopy} from "../../ui/PageFrame.js";
import {isBindingAction, applicationBindings} from "../../input/router.js";
import React, { memo, useCallback, useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../../input/runtime.js";
import { configGet, configSet, detectTerminals, testWebSearchConfig } from "../../backend/tools.js";
import type { TerminalInventory, TerminalOption } from "../../backend/terminalLauncher.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { colors, padEndWidth, usePanelSize } from "../../theme/index.js";
import {FormFieldRow, fieldLabelWidth} from "../FormFieldRow.js";
import {cycleChoiceIndex} from "../../input/choice.js";
import type { ConfigTabProps } from "./types.js";
import { notifyGeneralConfigChanged } from "../../hooks/useGeneralConfig.js";
import { applyTextOp } from "../../hooks/useTextInput.js";
import { productVersion } from "../../backend/update.js";
import {useFeedback} from "../../hooks/useFeedback.js";

type SettingId = "verbose" | "language" | "auto_compact" | "agent_routing" | "external_editor" | "web_search_mode" | "web_search_engine" | "web_search_endpoint" | "terminal" | "prompt_profile" | "diff_layout";
type Settings = {
  verbose: boolean;
  diff_layout: "split" | "unified";
  language: "en" | "zh";
  auto_compact: boolean;
  agent_routing: "owner" | "fresh";
  external_editor: string;
  web_search_mode: "auto" | "provider" | "searxng" | "disabled";
  web_search_endpoint: string;
  web_search_engine: "google" | "bing" | "baidu" | "yandex" | "duckduckgo";
  terminal: string;
  prompt_profile: string;
};


const OPTIONS: Record<Exclude<SettingId, "external_editor" | "web_search_endpoint" | "terminal" | "prompt_profile">, readonly (string | number | boolean)[]> = {
  verbose: [false, true],
  diff_layout: ["split", "unified"],
  language: ["en", "zh"],
  auto_compact: [false, true],
  agent_routing: ["owner", "fresh"],
  web_search_mode: ["auto", "provider", "searxng", "disabled"],
  web_search_engine: ["google", "bing", "baidu", "yandex", "duckduckgo"],
};
const TEXT_SETTINGS = new Set<SettingId>(["external_editor", "web_search_endpoint"]);
type TextSettingId = "external_editor" | "web_search_endpoint";
function isTextSetting(id: SettingId): id is TextSettingId {
  return TEXT_SETTINGS.has(id);
}

export const GeneralTab = memo(function GeneralTab({
  client, project, cmdInput, onFooter, onStatusUpdate, report, shell, contentFocused, initialSetting, onSettingSaved,
}: ConfigTabProps) {
  const t=useCopy();
  const {w} = usePanelSize({widthOffset: 4});
  const ROWS: { id: SettingId; label: string }[] = [
    { id: "verbose", label: t({en:"Verbose",zh:"详细显示"}) },
    { id: "language", label: t({en:"Language",zh:"语言"}) },
    { id: "auto_compact", label: t({en:"Auto Compact",zh:"自动压缩"}) },
    { id: "agent_routing", label: t({en:"Agent Routing",zh:"Agent 路由"}) },
    { id: "external_editor", label: t({en:"External Editor",zh:"外部编辑器"}) },
    { id: "web_search_mode", label: t({en:"Web Search",zh:"网页搜索"}) },
    { id: "web_search_engine", label: t({en:"Fallback Engine",zh:"备用搜索引擎"}) },
    { id: "web_search_endpoint", label: t({en:"SearXNG Fallback",zh:"SearXNG 备用端点"}) },
    { id: "terminal", label: t({en:"Terminal",zh:"终端"}) },
    { id: "prompt_profile", label: t({en:"Prompt Instance",zh:"提示词实例"}) },
    { id: "diff_layout", label: t({en:"Diff Layout",zh:"差异布局"}) },
  ];

  const [settings, setSettings] = useState<Settings>({
    verbose: false, diff_layout: "split", language: "en", auto_compact: true, agent_routing: "owner",
    external_editor: "", web_search_mode: "auto", web_search_endpoint: "",
    web_search_engine: "duckduckgo", terminal: "auto", prompt_profile: "default",
  });
  const [profiles, setProfiles] = useState<{id: string; label: string}[]>([]);
  const [row, setRow] = useState(Math.max(0, ROWS.findIndex(r => r.id === initialSetting)));
  const [editing, setEditing] = useState<SettingId | null>(initialSetting === "terminal" ? "terminal" : null);
  const [draft, setDraft] = useState<string | number | boolean>("");
  const [saving, setSaving] = useState<SettingId | null>(null);
  const feedback = useFeedback({kind: "page", id: `config/general:${project}`});
  const message = feedback.text, setMessage = feedback.show;
  const [terminals, setTerminals] = useState<TerminalOption[]>([]);
  const [terminalWarning, setTerminalWarning] = useState("");
  const [detectingTerminals, setDetectingTerminals] = useState(true);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [version, setVersion] = useState(t({en:"Loading…",zh:"正在读取…"}));
  useEffect(() => {
    let active = true;
    setVersion(t({en:"Loading…",zh:"正在读取…"}));
    productVersion(client).then(value => { if (active) setVersion(value.version); })
      .catch(() => { if (active) setVersion(t({en:"Unavailable · reopen General to retry",zh:"不可用，请重新打开通用设置重试"})); });
    return () => { active = false; };
  }, [client, t]);
  const refreshTerminals = useCallback(async () => {
    setDetectingTerminals(true);
    try {
      const inventory = await detectTerminals(client) as TerminalInventory;
      if (!Array.isArray(inventory?.options)) throw new Error("Terminal inventory unavailable");
      setTerminals(inventory.options);
      setTerminalWarning((inventory.warnings || []).join(" "));
      return inventory;
    } catch (error) {
      setTerminalWarning(`Terminal detection failed: ${String(error)} · R retry`);
      return undefined;
    } finally { setDetectingTerminals(false); }
  }, [client]);
  useEffect(() => { void refreshTerminals(); }, [refreshTerminals]);
  // The dot describes the complete runnable search profile, not merely a
  // syntactically valid engine name. Without an endpoint the tool is offline,
  // never an ambiguous gray "unknown" success.
  const [searchHealth, setSearchHealth] = useState<"testing" | "online" | "offline">("offline");

  const testSearch = useCallback(async () => {
    setSearchHealth("testing");
    try {
      const result: any = await testWebSearchConfig(client);
      setSearchHealth(result?.reachable ? "online" : "offline");
      return Boolean(result?.reachable);
    } catch {
      setSearchHealth("offline");
      return false;
    }
  }, [client]);

  useEffect(() => {
    let active = true;
    configGet(client).then((result: any) => {
      if (!active) return;
      setProfiles(Array.isArray(result?.prompt_profiles) ? result.prompt_profiles : []);
      setSettings({
        prompt_profile: String(result?.prompt_profile || "default"),
        verbose: Boolean(result?.verbose),
        diff_layout: result?.diff_layout === "unified" ? "unified" : "split",
        language: result?.language === "zh" ? "zh" : "en",
        auto_compact: result?.auto_compact !== false,
        agent_routing: result?.agent_routing === "fresh" ? "fresh" : "owner",
        external_editor: String(result?.external_editor || ""),
        web_search_mode: (["auto", "provider", "searxng", "disabled"].includes(String(result?.web_search_mode))
          ? String(result.web_search_mode) : "auto") as Settings["web_search_mode"],
        web_search_endpoint: String(result?.web_search_endpoint || ""),
        web_search_engine: (["google", "bing", "baidu", "yandex", "duckduckgo"].includes(String(result?.web_search_engine))
          ? String(result.web_search_engine) : "duckduckgo") as Settings["web_search_engine"],
        terminal: String(result?.launcher?.terminal || "auto"),
      });
      if (initialSetting === "terminal") setDraft(String(result?.launcher?.terminal || "auto"));
      setSettingsLoaded(true);
      if (result?.web_search_endpoint) void testSearch();
      else setSearchHealth("offline");
    }).catch((error) => { if (active) { setMessage(`Config load failed: ${String(error)} · reopen General to retry`); setSettingsLoaded(false); } });
    return () => { active = false; };
  }, [client, testSearch, initialSetting]);

  useEffect(() => { report({ sub: editing !== null, fullscreen: false }); }, [editing, report]);
  useEffect(() => {
    if (editing && isTextSetting(editing)) {
      onFooter({kind: "normal", cmdInput, statusText: t({en:"Enter save · Esc cancel",zh:"Enter 保存 · Esc 取消",keys:true}),
        suggestions: [], suggestionIdx: 0, cmdResult: ""});
    } else onFooter({ hidden: true });
    return () => onFooter(null);
  }, [onFooter, editing, cmdInput.value, cmdInput.cursor, cmdInput]);
  useEffect(() => { onStatusUpdate?.(message); }, [message, onStatusUpdate]);
  useEffect(() => () => onStatusUpdate?.(""), [onStatusUpdate]);

  const save = useCallback(async (id: SettingId, value: string | number | boolean) => {
    setSaving(id);
    setMessage(`Saving ${ROWS.find((item) => item.id === id)?.label || id}…`, {retention: {kind: "until-resolved", reason: "operation-pending"}});
    try {
      const key = id === "terminal" ? "launcher.terminal" : id;
      await configSet(client, key, value);
      notifyGeneralConfigChanged();
      setSettings((current) => ({ ...current, [id]: value } as Settings));
      const effectiveEndpoint = String(
        id === "web_search_endpoint" ? value : settings.web_search_endpoint || "",
      ).trim();
      if ((id === "web_search_endpoint" || id === "web_search_engine")
          && effectiveEndpoint) {
        const reachable = await testSearch();
        setMessage(reachable ? "Saved · search endpoint reachable" : "Saved · search endpoint unavailable");
      } else {
        if (id === "web_search_endpoint" || id === "web_search_engine") {
          setSearchHealth("offline");
          setMessage("Saved · configure a SearXNG endpoint to enable search");
        } else setMessage(id === "terminal" ? "Saved · applies on the next launch" :
                          id === "prompt_profile" ? "Saved · new runs use this instance; active runs keep their snapshot" : "Saved");
      }
      const notice = await onSettingSaved?.(key);
      if (notice) setMessage(notice);
      setEditing(null);
    } catch (error) {
      setMessage(`Save failed: ${String(error)}`, {tone: "error"});
    } finally {
      setSaving(null);
    }
  }, [client, testSearch, settings.web_search_endpoint, onSettingSaved]);

  useInput((input, key) => {
    if (!contentFocused) return false;
    if (saving) return;
    if ((editing === "terminal" || ROWS[row]?.id === "terminal") && isBindingAction("terminal.rescan", input, key)) {
      void refreshTerminals(); return;
    }
    if (editing) {
      if (matchChord("escape", input, key)) { setEditing(null); cmdInput.setValue(""); return; }
      if (isTextSetting(editing)) {
        if (matchChord("enter", input, key)) { void save(editing, cmdInput.value); return; }
        if (matchChord("backspace", input, key)) applyTextOp({op: "delete_back"}, cmdInput);
        else if (matchChord("delete", input, key)) applyTextOp({op: "delete_forward"}, cmdInput);
        else if (matchChord("left", input, key)) applyTextOp({op: "move_cursor", delta: -1}, cmdInput);
        else if (matchChord("right", input, key)) applyTextOp({op: "move_cursor", delta: 1}, cmdInput);
        else if (matchChord("home", input, key)) applyTextOp({op: "move_to_start"}, cmdInput);
        else if (matchChord("end", input, key)) applyTextOp({op: "move_to_end"}, cmdInput);
        else if (input && !key.ctrl && !key.meta) applyTextOp({op: "insert", text: input}, cmdInput);
        return;
      }
      const options = editing === "terminal" ? terminals.filter(t => t.available).map(t => t.id) :
                      editing === "prompt_profile" ? profiles.map(p => p.id) : OPTIONS[editing];
      if (editing === "prompt_profile" && (!settingsLoaded || !options.length)) return;
      if (editing === "terminal" && (!settingsLoaded || detectingTerminals || options.length === 0)) return;
      const index = Math.max(0, options.indexOf(draft));
      if (matchChord("left", input, key)) {
        setDraft(options[cycleChoiceIndex(index, -1, options.length)]!); return;
      }
      if (matchChord("right", input, key)) {
        setDraft(options[cycleChoiceIndex(index, 1, options.length)]!); return;
      }
      if (matchChord("enter", input, key)) void save(editing, draft);
      return;
    }
    if (matchChord("escape", input, key)) { shell.leaveContent(); return; }
    if (matchChord("up", input, key)) { setRow((value) => Math.max(0, value - 1)); return; }
    if (matchChord("down", input, key)) { setRow((value) => Math.min(ROWS.length - 1, value + 1)); return; }
    if (ROWS[row]?.id === "terminal" && (matchChord("left", input, key) || matchChord("right", input, key))) {
      const options = terminals.filter(t => t.available).map(t => t.id);
      if (detectingTerminals || !options.length) return;
      const index = Math.max(0, options.indexOf(settings.terminal));
      setDraft(options[cycleChoiceIndex(index, key.leftArrow ? -1 : 1, options.length)]!);
      setEditing("terminal"); return;
    }
    if (!matchChord("enter", input, key)) return;
    const id = ROWS[row]!.id;
    setEditing(id);
    setDraft(settings[id]);
    if (isTextSetting(id)) cmdInput.setValue(String(settings[id] || ""));
  }, {bindingContext: contentFocused && (editing === "terminal" || (!editing && ROWS[row]?.id === "terminal")) ? "terminal" : ""});

  const valueLabel = (id: SettingId) => {
    const value = editing === id ? draft : settings[id];
    if (saving === id) return t({en:"SAVING",zh:"正在保存"});
    if (id === "prompt_profile") return profiles.find(p => p.id === value)?.label || String(value);
    if (id === "terminal") {
      if (detectingTerminals) return t({en:"Detecting installed terminals…",zh:"正在检测已安装终端…"});
      const option = terminals.find(t => t.id === value);
      return option ? (option.id === "auto" ? t({en:"Automatic",zh:"自动选择"}) : option.id === "current" ? t({en:"Current terminal",zh:"当前终端"}) : option.label) + (option.available ? "" : t({en:" · unavailable",zh:" · 不可用"})) : String(value || t({en:"Not selected",zh:"未选择"}));
    }
    if (id === "verbose" || id === "auto_compact") return value ? "ON" : "OFF";
    if (id === "diff_layout") return value === "unified" ? t({en:"Unified",zh:"统一"}) : t({en:"Split",zh:"并排"});
    if (id === "language") return value === "zh" ? "中文" : "English";
    if (id === "agent_routing") return value === "fresh" ? t({en:"Fresh agent",zh:"新进程"}) : t({en:"Original owner",zh:"原负责进程"});
    if (id === "web_search_mode") return value === "auto" ? t({en:"Provider → SearXNG",zh:"服务商 → SearXNG"}) : value === "disabled" ? t({en:"Disabled",zh:"已禁用"}) : value === "provider" ? t({en:"Provider",zh:"服务商"}) : String(value);
    if (id === "web_search_engine") return String(value).replace(/^./, char => char.toUpperCase());
    if (isTextSetting(id)) {
      const fallback = id === "web_search_endpoint" ? t({en:"Not configured",zh:"未配置"}) : t({en:"System default",zh:"系统默认"});
      const text = String(editing === id ? cmdInput.value : value || fallback);
      return text.length > 58 ? "…" + text.slice(-57) : text;
    }
    return String(value);
  };

  const labelWidth = fieldLabelWidth(["Gitgo Version", ...ROWS.map(item => item.label)], Math.max(1, w - 10), "");
  return (
    <Box flexDirection="column" flexGrow={1}>
      <Text dimColor>{padEndWidth("Gitgo Version", labelWidth)}{version} {t({en:" (read-only)",zh:" （只读）"})}</Text>
      {ROWS.map((item, index) => {
        const selected = contentFocused && index === row;
        const isEditing = editing === item.id;
        return (
          <FormFieldRow key={item.id} label={item.label} labelWidth={labelWidth} suffix="" highlight={false} selected={selected} editing={isEditing}>
            <SelectionText selected={selected}  >
              {item.id === "web_search_engine" ? <Text color={searchHealth === "online" ? colors.success : searchHealth === "offline" ? colors.danger : colors.named.gray}>● </Text> : null}
              {valueLabel(item.id)}
            </SelectionText>
          </FormFieldRow>
        );
      })}
      <Text dimColor>
        {editing
          ? isTextSetting(editing) ? t({en:"Enter save · Esc cancel",zh:"Enter 保存 · Esc 取消",keys:true}) : t({en:"←/→ choose · Enter save · Esc cancel",zh:"←/→ 选择 · Enter 保存 · Esc 取消",keys:true})
          : contentFocused
            ? t({en:"↑↓ select · Enter change · Esc tabs",zh:"↑↓ 选择 · Enter 修改 · Esc 页签",keys:true})
            : t({en:"←/→ tabs · Enter open · Esc back",zh:"←/→ 页签 · Enter 打开 · Esc 返回",keys:true})}
      </Text>
      {ROWS[row]?.id === "terminal" && <Text dimColor>
        {initialSetting === "terminal" ? t({en:"First launch · choose a terminal and press Enter. ",zh:"首次启动：选择终端并按 Enter。 ",keys:true}) : t({en:"Changes apply on the next launch. ",zh:"更改在下次启动时生效。 "})}{applicationBindings.label("terminal.rescan")} {t({en:"rescan",zh:"重新扫描"})}</Text>}
      {terminalWarning && ROWS[row]?.id === "terminal" ? <Text color={colors.warning}>{terminalWarning}</Text> : null}
    </Box>
  );
});
