// src/components/CreateProjectPanel.tsx — /create form panel
// Field labels render here; the single text input lives in the bottom CommandBar
// (shared cmdInput via FooterConfig, same as /config). Tab/↑↓ switches fields,
// preserving each field's text in local state; ←/→ cycles the LLM provider.
import React, { memo, useState, useEffect } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { useSelectionStyle } from "../theme/index.js";
import { createProject } from "../mcp/tools.js";
import { chordLabel } from "../input/bindings.js";
import { resolveCreateProjectKey } from "../input/overlays/createProject.js";
import { applyTextOp, type UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";

type Props = {
  client: McpClient;
  defaultWorkspace?: string;
  onDismiss: () => void;
  onCreated?: (name: string) => void;
  cmdInput: UseTextInputReturn;
  onFooter: (cfg: FooterConfig | null) => void;
};

type Field = "name" | "workspace" | "release" | "llm";
const FIELDS: Field[] = ["name", "workspace", "release", "llm"];
const FIELD_LABELS: Record<Field, string> = {
  name: "Name *",
  workspace: "Workspace Path *",
  release: "Release URL",
  llm: "LLM Provider",
};
const LLM_OPTIONS = ["use default", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-7"];

const HINT = `${chordLabel("tab")} next    ${chordLabel("upDown")} switch    ${chordLabel("enter")} confirm`;

export const CreateProjectPanel = memo(function CreateProjectPanel({
  client, defaultWorkspace, onDismiss, onCreated, cmdInput, onFooter,
}: Props) {
  const [values, setValues] = useState<{ name: string; workspace: string; release: string }>({
    name: "",
    workspace: defaultWorkspace || "",
    release: "",
  });
  const [llmProvider, setLlmProvider] = useState("use default");
  const [fieldIdx, setFieldIdx] = useState(0);

  const activeField = FIELDS[fieldIdx];
  const isActive = (f: Field) => FIELDS[fieldIdx] === f;

  // Persist the active editable field's buffer back to `values` before switching.
  const commitActive = () => {
    if (activeField === "name") setValues((v) => ({ ...v, name: cmdInput.value }));
    else if (activeField === "workspace") setValues((v) => ({ ...v, workspace: cmdInput.value }));
    else if (activeField === "release") setValues((v) => ({ ...v, release: cmdInput.value }));
  };

  const next = () => { commitActive(); setFieldIdx((f) => (f + 1) % FIELDS.length); };
  const prev = () => { commitActive(); setFieldIdx((f) => (f - 1 + FIELDS.length) % FIELDS.length); };

  // Load the newly-active field into the shared buffer.
  useEffect(() => {
    const v = activeField === "llm" ? llmProvider
      : activeField === "name" ? values.name
      : activeField === "workspace" ? values.workspace
      : values.release;
    cmdInput.setValue(v);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldIdx, llmProvider]);

  // Push the shared buffer into the bottom CommandBar.
  useEffect(() => {
    onFooter({
      kind: "normal",
      cmdInput,
      statusText: HINT,
      suggestions: [],
      suggestionIdx: 0,
      cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput.value, cmdInput.cursor, onFooter]);

  const fieldValue = (f: Field): string =>
    f === "llm" ? llmProvider
    : f === "name" ? values.name
    : f === "workspace" ? values.workspace
    : values.release;

  const submit = () => {
    // Merge the active field's live buffer into values before reading.
    const final = {
      ...values,
      ...(activeField === "name" ? { name: cmdInput.value } : {}),
      ...(activeField === "workspace" ? { workspace: cmdInput.value } : {}),
      ...(activeField === "release" ? { release: cmdInput.value } : {}),
    };
    const name = final.name.trim();
    const workspace = final.workspace.trim();
    if (!name || !workspace) return; // name + workspace are required
    createProject(client, {
      name,
      workspace_path: workspace,
      release_url: final.release.trim(),
      llm_provider: llmProvider !== "use default" ? llmProvider : "",
    }).then(() => onCreated?.(name)).catch(() => {});
    onDismiss();
  };

  useInput((input: string, key: any) => {
    for (const a of resolveCreateProjectKey(activeField, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "nextField": next(); break;
        case "prevField": prev(); break;
        case "submit": submit(); break;
        case "llmPrev":
          setLlmProvider(LLM_OPTIONS[(LLM_OPTIONS.indexOf(llmProvider) - 1 + LLM_OPTIONS.length) % LLM_OPTIONS.length]);
          break;
        case "llmNext":
          setLlmProvider(LLM_OPTIONS[(LLM_OPTIONS.indexOf(llmProvider) + 1) % LLM_OPTIONS.length]);
          break;
        case "text": applyTextOp(a.op, cmdInput); break;
      }
    }
  });

  return (
    <Box flexDirection="column" padding={1} flexGrow={1}>
      <Box marginBottom={1}>
        <Text bold>Create Project</Text>
        <Text dimColor>{"    * required    "}{chordLabel("escape")} cancel</Text>
      </Box>

      {/* Field label list — active field highlighted with row selection */}
      <Box flexDirection="column" marginBottom={1}>
        {FIELDS.map((f) => {
          const active = isActive(f);
          const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
          return (
            <Box key={f} flexDirection="row">
              <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                {FIELD_LABELS[f].padEnd(17)}
              </Text>
              <Text dimColor backgroundColor={style.bg}>
                {active ? " " : (fieldValue(f) || "—")}
              </Text>
            </Box>
          );
        })}
      </Box>

      <Box marginTop={1}>
        <Text dimColor>{HINT}</Text>
      </Box>
    </Box>
  );
});
