// src/components/config/PublishTab.tsx — /config Publish tab (self-contained).
// Owns: commit templates (list/add/edit/delete) + push settings (privacy toggle).
// No COMMAND bar (publish has no footer), so no command-mode key handling.

import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import {
  configSet, templateList, templateAdd, templateEdit, templateDelete,
} from "../../mcp/tools.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { colors, useSelectionStyle, sortByName, placeholderChar } from "../../theme/index.js";
import type { ConfigTabProps } from "./types.js";

type PublishView = "menu" | "templates" | "push";

const PUBLISH_MENU: { id: Exclude<PublishView, "menu">; label: string }[] = [
  { id: "templates", label: "Templates" },
  { id: "push", label: "Push" },
];

type PublishCtx = {
  publishView: PublishView;
  templateEditMode: "idle" | "add" | "edit";
};

type PublishAction =
  | { type: "esc" }
  | { type: "tabPrev" }
  | { type: "tabNext" }
  | { type: "publishMenuMove"; delta: number }
  | { type: "publishMenuConfirm" }
  | { type: "tplMove"; delta: number }
  | { type: "tplNew" }
  | { type: "tplEdit" }
  | { type: "tplDelete" }
  | { type: "tplEditSave" }
  | { type: "tplEditBackspace" }
  | { type: "tplEditInsert"; text: string }
  | { type: "tplEditCancel" }
  | { type: "tplEditSwitchField" }
  | { type: "pushMove"; delta: number }
  | { type: "pushToggle" };

function resolvePublishKey(ctx: PublishCtx, input: string, key: any): PublishAction[] {
  // Template edit mode
  if (ctx.templateEditMode !== "idle") {
    if (matchChord("escape", input, key)) return [{ type: "tplEditCancel" }];
    if (matchChord("enter", input, key)) return [{ type: "tplEditSave" }];
    if (matchChord("tabAny", input, key)) return [{ type: "tplEditSwitchField" }];
    if (matchChord("backspace", input, key) || matchChord("delete", input, key)) return [{ type: "tplEditBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "tplEditInsert", text: input }];
    }
    return [];
  }

  // Esc (list mode)
  if (matchChord("escape", input, key)) return [{ type: "esc" }];

  // Tab switching via left/right
  if (matchChord("left", input, key)) return [{ type: "tabPrev" }];
  if (matchChord("right", input, key)) return [{ type: "tabNext" }];

  // Per-view content
  if (ctx.publishView === "menu") {
    if (matchChord("up", input, key)) return [{ type: "publishMenuMove", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "publishMenuMove", delta: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "publishMenuConfirm" }];
    return [];
  }
  if (ctx.publishView === "templates") {
    if (matchChord("up", input, key)) return [{ type: "tplMove", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "tplMove", delta: 1 }];
    if (matchChord("letterN", input, key)) return [{ type: "tplNew" }];
    if (matchChord("letterE", input, key)) return [{ type: "tplEdit" }];
    if (matchChord("letterD", input, key)) return [{ type: "tplDelete" }];
    return [];
  }
  // push
  if (matchChord("up", input, key)) return [{ type: "pushMove", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "pushMove", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "pushToggle" }];
  return [];
}

export const PublishTab = memo(function PublishTab({
  client, cmdInput, onFooter, report, shell,
}: ConfigTabProps) {
  const [templates, setTemplates] = useState<{ name: string; description: string }[]>([]);
  const [templateIdx, setTemplateIdx] = useState(0);
  const [templateEditMode, setTemplateEditMode] = useState<"idle" | "add" | "edit">("idle");
  const [templateEditName, setTemplateEditName] = useState("");
  const [templateEditDesc, setTemplateEditDesc] = useState("");
  const [templateEditField, setTemplateEditField] = useState<"name" | "desc">("name");

  const [privacyOn, setPrivacyOn] = useState(true);
  const [pushFieldIdx, setPushFieldIdx] = useState(0);
  const [publishView, setPublishView] = useState<PublishView>("menu");
  const [publishMenuIdx, setPublishMenuIdx] = useState(0);

  const refreshTemplates = useCallback(() => {
    templateList(client).then((r: any) => {
      if (Array.isArray(r)) setTemplates(sortByName(r));
    }).catch(() => {});
  }, [client]);

  useEffect(() => {
    refreshTemplates();
  }, [refreshTemplates]);

  // Report sub (template edit) to shell.
  useEffect(() => {
    report({ sub: templateEditMode !== "idle", fullscreen: false });
  }, [report, templateEditMode]);

  // Footer: publish tab has NO command bar.
  useEffect(() => {
    onFooter({ hidden: true });
    return () => onFooter(null);
  }, [onFooter]);

  // ── Keyboard ────────────────────────────────────────────
  useInput((input: string, key: any) => {
    const ctx: PublishCtx = {
      publishView,
      templateEditMode,
    };
    for (const a of resolvePublishKey(ctx, input, key)) {
      switch (a.type) {
        case "esc":
          if (cmdInput.value.length > 0) {
            cmdInput.setValue("");
          } else if (publishView !== "menu") {
            setPublishView("menu");
          } else {
            shell.goToTab("providers");
          }
          break;
        case "tabPrev":
          shell.tabPrev();
          break;
        case "tabNext":
          shell.tabNext();
          break;
        case "publishMenuMove":
          setPublishMenuIdx((s) => (s + a.delta + PUBLISH_MENU.length) % PUBLISH_MENU.length);
          break;
        case "publishMenuConfirm": {
          const item = PUBLISH_MENU[publishMenuIdx];
          if (item) setPublishView(item.id);
          break;
        }
        case "tplMove":
          setTemplateIdx((s) => Math.max(0, Math.min(templates.length - 1, s + a.delta)));
          break;
        case "tplNew":
          setTemplateEditMode("add");
          setTemplateEditName("");
          setTemplateEditDesc("");
          setTemplateEditField("name");
          break;
        case "tplEdit": {
          const t = templates[templateIdx];
          if (t) {
            setTemplateEditMode("edit");
            setTemplateEditName(t.name);
            setTemplateEditDesc(t.description || "");
            setTemplateEditField("name");
          }
          break;
        }
        case "tplDelete": {
          const t = templates[templateIdx];
          if (t && t.name !== "default") {
            templateDelete(client, t.name)
              .then(() => {
                setTemplateIdx((s) => Math.min(s, templates.length - 2));
                refreshTemplates();
              }).catch(() => {});
          }
          break;
        }
        case "tplEditSave": {
          const n = templateEditName.trim();
          if (!n) break;
          const d = templateEditDesc.trim();
          if (templateEditMode === "add") {
            templateAdd(client, { name: n, description: d, header_format: "", body_format: "" })
              .then(() => refreshTemplates()).catch(() => {});
          } else {
            templateEdit(client, n, d)
              .then(() => refreshTemplates()).catch(() => {});
          }
          setTemplateEditMode("idle");
          break;
        }
        case "tplEditBackspace":
          if (templateEditField === "name") setTemplateEditName((s) => s.slice(0, -1));
          else setTemplateEditDesc((s) => s.slice(0, -1));
          break;
        case "tplEditInsert":
          if (templateEditField === "name") setTemplateEditName((s) => s + a.text);
          else setTemplateEditDesc((s) => s + a.text);
          break;
        case "tplEditCancel":
          setTemplateEditMode("idle");
          break;
        case "tplEditSwitchField":
          setTemplateEditField((f) => (f === "name" ? "desc" : "name"));
          break;
        case "pushMove":
          setPushFieldIdx((s) => Math.max(0, Math.min(2, s + a.delta)));
          break;
        case "pushToggle":
          if (pushFieldIdx === 0) {
            setPrivacyOn((p) => {
              const next = !p;
              configSet(client, "publish.privacy_clean", next).catch(() => {});
              return next;
            });
          }
          break;
      }
    }
  });

  // ── Render ──────────────────────────────────────────────
  if (publishView === "menu") {
    return (
      <Box flexDirection="column" flexGrow={1}>
        {PUBLISH_MENU.map((item, i) => {
          const active = i === publishMenuIdx;
          const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
          return (
            <Text key={item.id} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
              {item.label}
            </Text>
          );
        })}
        <Box marginTop={1}>
          <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} open    {chordLabel("escape")} back</Text>
        </Box>
      </Box>
    );
  }

  if (publishView === "templates") {
    return (
      <Box flexDirection="column" flexGrow={1}>
        {templateEditMode !== "idle" && (
          <Box flexDirection="column" marginBottom={1}>
            <Box flexDirection="row">
              <Text dimColor>Name: </Text>
              {(() => {
                const s = useSelectionStyle(templateEditField === "name" ? "focused" : "non-focused", "edit-field");
                return (
                  <Text color={s.fg} backgroundColor={s.bg} bold={s.bold}>
                    {templateEditName}
                  </Text>
                );
              })()}
              {templateEditField === "name" ? <Text color={colors.edit.placeholder.color}>{placeholderChar(true)}</Text> : null}
            </Box>
            <Box flexDirection="row" marginTop={1}>
              <Text dimColor>Desc: </Text>
              {(() => {
                const s = useSelectionStyle(templateEditField === "desc" ? "focused" : "non-focused", "edit-field");
                return (
                  <Text color={s.fg} backgroundColor={s.bg} bold={s.bold}>
                    {templateEditDesc}
                  </Text>
                );
              })()}
              {templateEditField === "desc" ? <Text color={colors.edit.placeholder.color}>{placeholderChar(true)}</Text> : null}
            </Box>
            <Text dimColor>{chordLabel("tab")} switch field    {chordLabel("enter")} save    {chordLabel("escape")} cancel</Text>
          </Box>
        )}

        {templateEditMode === "idle" && (
          <Box flexDirection="column" marginBottom={1}>
            <Text dimColor bold>▸ Templates</Text>
            {templates.length === 0 ? (
              <Text dimColor>No templates defined</Text>
            ) : (
              templates.map((t, i) => {
                const active = i === templateIdx;
                const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
                return (
                  <Box key={t.name} flexDirection="row">
                    <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>{t.name}</Text>
                    {active && t.description ? <Text dimColor>  {t.description}</Text> : null}
                  </Box>
                );
              })
            )}
            <Text dimColor>{chordLabel("letterN")} new    {chordLabel("letterE")} edit    {chordLabel("letterD")} delete    {chordLabel("escape")} back</Text>
          </Box>
        )}
      </Box>
    );
  }

  // publishView === "push"
  return (
    <Box flexDirection="column" flexGrow={1}>
      <Box flexDirection="column">
        <Text dimColor bold>▸ Push</Text>
        {([
          { key: "privacy", label: `Privacy Clean: ${privacyOn ? "ON" : "OFF"}`, value: "" },
          { key: "format", label: "Commit Format", value: "[project-prefix] brief description" },
          { key: "release", label: "Release URL", value: "configured per project in LLM section" },
        ] as const).map((row, i) => {
          const active = pushFieldIdx === i;
          const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
          return (
            <Box key={row.key} flexDirection="row">
              <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>{row.label}</Text>
              {row.value ? <Text dimColor>  {row.value}</Text> : null}
            </Box>
          );
        })}
        <Text dimColor>{chordLabel("upDown")} select field    {chordLabel("enter")} toggle privacy    {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
