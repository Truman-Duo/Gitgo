import React, { memo, useCallback, useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../../input/runtime.js";
import { configGet, configSet, testWebSearchConfig } from "../../backend/tools.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { colors } from "../../theme/index.js";
import type { ConfigTabProps } from "./types.js";
import { notifyGeneralConfigChanged } from "../../hooks/useGeneralConfig.js";
import { applyTextOp } from "../../hooks/useTextInput.js";

type SettingId = "verbose" | "language" | "auto_compact" | "agent_routing" | "external_editor" | "web_search_mode" | "web_search_engine" | "web_search_endpoint";
type Settings = {
  verbose: boolean;
  language: "en" | "zh";
  auto_compact: boolean;
  agent_routing: "owner" | "fresh";
  external_editor: string;
  web_search_mode: "auto" | "provider" | "searxng" | "disabled";
  web_search_endpoint: string;
  web_search_engine: "google" | "bing" | "baidu" | "yandex" | "duckduckgo";
};

const ROWS: { id: SettingId; label: string }[] = [
  { id: "verbose", label: "Verbose" },
  { id: "language", label: "Language" },
  { id: "auto_compact", label: "Auto Compact" },
  { id: "agent_routing", label: "Agent Routing" },
  { id: "external_editor", label: "External Editor" },
  { id: "web_search_mode", label: "Web Search" },
  { id: "web_search_engine", label: "Fallback Engine" },
  { id: "web_search_endpoint", label: "SearXNG Fallback" },
];
const OPTIONS: Record<Exclude<SettingId, "external_editor" | "web_search_endpoint">, readonly (string | number | boolean)[]> = {
  verbose: [false, true],
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
  client, cmdInput, onFooter, onStatusUpdate, report, shell, contentFocused,
}: ConfigTabProps) {
  const [settings, setSettings] = useState<Settings>({
    verbose: false, language: "en", auto_compact: true, agent_routing: "owner",
    external_editor: "", web_search_mode: "auto", web_search_endpoint: "",
    web_search_engine: "duckduckgo",
  });
  const [row, setRow] = useState(0);
  const [editing, setEditing] = useState<SettingId | null>(null);
  const [draft, setDraft] = useState<string | number | boolean>("");
  const [saving, setSaving] = useState<SettingId | null>(null);
  const [message, setMessage] = useState("");
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
      setSettings({
        verbose: Boolean(result?.verbose),
        language: result?.language === "zh" ? "zh" : "en",
        auto_compact: result?.auto_compact !== false,
        agent_routing: result?.agent_routing === "fresh" ? "fresh" : "owner",
        external_editor: String(result?.external_editor || ""),
        web_search_mode: (["auto", "provider", "searxng", "disabled"].includes(String(result?.web_search_mode))
          ? String(result.web_search_mode) : "auto") as Settings["web_search_mode"],
        web_search_endpoint: String(result?.web_search_endpoint || ""),
        web_search_engine: (["google", "bing", "baidu", "yandex", "duckduckgo"].includes(String(result?.web_search_engine))
          ? String(result.web_search_engine) : "duckduckgo") as Settings["web_search_engine"],
      });
      if (result?.web_search_endpoint) void testSearch();
      else setSearchHealth("offline");
    }).catch((error) => { if (active) setMessage(String(error)); });
    return () => { active = false; };
  }, [client, testSearch]);

  useEffect(() => { report({ sub: editing !== null, fullscreen: false }); }, [editing, report]);
  useEffect(() => {
    if (editing && isTextSetting(editing)) {
      onFooter({kind: "normal", cmdInput, statusText: "Enter save · Esc cancel",
        suggestions: [], suggestionIdx: 0, cmdResult: ""});
    } else onFooter({ hidden: true });
    return () => onFooter(null);
  }, [onFooter, editing, cmdInput.value, cmdInput.cursor, cmdInput]);
  useEffect(() => { onStatusUpdate?.(message); }, [message, onStatusUpdate]);

  const save = useCallback(async (id: SettingId, value: string | number | boolean) => {
    setSaving(id);
    setMessage(`Saving ${ROWS.find((item) => item.id === id)?.label || id}…`);
    try {
      await configSet(client, id, value);
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
        } else setMessage("Saved");
      }
      setEditing(null);
    } catch (error) {
      setMessage(`Save failed: ${String(error)}`);
    } finally {
      setSaving(null);
    }
  }, [client, testSearch, settings.web_search_endpoint]);

  useInput((input, key) => {
    if (!contentFocused) return false;
    if (saving) return;
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
      const options = OPTIONS[editing];
      const index = Math.max(0, options.indexOf(draft));
      if (matchChord("left", input, key)) {
        setDraft(options[(index + options.length - 1) % options.length]!); return;
      }
      if (matchChord("right", input, key)) {
        setDraft(options[(index + 1) % options.length]!); return;
      }
      if (matchChord("enter", input, key)) void save(editing, draft);
      return;
    }
    if (matchChord("escape", input, key)) { shell.leaveContent(); return; }
    if (matchChord("up", input, key)) { setRow((value) => Math.max(0, value - 1)); return; }
    if (matchChord("down", input, key)) { setRow((value) => Math.min(ROWS.length - 1, value + 1)); return; }
    if (!matchChord("enter", input, key)) return;
    const id = ROWS[row]!.id;
    setEditing(id);
    setDraft(settings[id]);
    if (isTextSetting(id)) cmdInput.setValue(String(settings[id] || ""));
  });

  const valueLabel = (id: SettingId) => {
    const value = editing === id ? draft : settings[id];
    if (saving === id) return "SAVING";
    if (id === "verbose" || id === "auto_compact") return value ? "ON" : "OFF";
    if (id === "language") return value === "zh" ? "中文" : "English";
    if (id === "agent_routing") return value === "fresh" ? "Fresh agent" : "Original owner";
    if (id === "web_search_mode") return value === "auto" ? "Provider → SearXNG" : String(value);
    if (id === "web_search_engine") return String(value).replace(/^./, char => char.toUpperCase());
    if (isTextSetting(id)) {
      const fallback = id === "web_search_endpoint" ? "Not configured" : "System default";
      const text = String(editing === id ? cmdInput.value : value || fallback);
      return text.length > 58 ? "…" + text.slice(-57) : text;
    }
    return String(value);
  };

  return (
    <Box flexDirection="column" flexGrow={1}>
      {ROWS.map((item, index) => {
        const selected = contentFocused && index === row;
        const isEditing = editing === item.id;
        return (
          <Box key={item.id} flexDirection="row">
            <Text bold={selected} color={selected ? colors.selection.row.fg : undefined}>
              {item.label.padEnd(18)}
            </Text>
            <Text bold={isEditing || selected} color={isEditing ? colors.selection.row.fg : undefined}>
              {item.id === "web_search_engine" ? (
                <Text color={searchHealth === "online" ? colors.success : searchHealth === "offline" ? colors.danger : colors.named.gray}>
                  ●{" "}
                </Text>
              ) : null}
              {valueLabel(item.id)}
            </Text>
          </Box>
        );
      })}
      <Text dimColor>
        {editing
          ? `${chordLabel("leftRight")} choose  ${chordLabel("enter")} save  ${chordLabel("escape")} cancel`
          : contentFocused
            ? `${chordLabel("upDown")} select  ${chordLabel("enter")} change  ${chordLabel("escape")} tabs`
            : `${chordLabel("leftRight")} tabs  ${chordLabel("enter")} open  ${chordLabel("escape")} back`}
      </Text>
    </Box>
  );
});
