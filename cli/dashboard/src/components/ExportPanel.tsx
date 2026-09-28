import React, { memo, useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { resolveExportKey } from "../input/overlays/export.js";
import { colors, useSelectionStyle } from "../theme/index.js";
import { exportData } from "../backend/tools.js";
import { chordLabel } from "../input/bindings.js";
import { applyTextOp, type UseTextInputReturn } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  cmdInput: UseTextInputReturn;
  onFooter: (config: FooterConfig | null) => void;
  onDismiss: () => void;
};

const FIELDS = ["scope", "format", "destination"] as const;
type Field = typeof FIELDS[number];
const SCOPES = ["Minimal", "Full"] as const;
const FORMATS = ["json", "yaml", "markdown"] as const;

export const ExportPanel = memo(function ExportPanel({
  client, project, cols, cmdInput, onFooter, onDismiss,
}: Props) {
  const [fieldIndex, setFieldIndex] = useState(0);
  const [scopeIndex, setScopeIndex] = useState(0);
  const [formatIndex, setFormatIndex] = useState(0);
  const [destination, setDestination] = useState(`.gitgo/exports/${project}-minimal.json`);
  const [status, setStatus] = useState<"idle" | "exporting" | "done" | "error">("idle");
  const [result, setResult] = useState("");
  const field = FIELDS[fieldIndex]!;

  useEffect(() => {
    if (field === "destination" && status !== "done") cmdInput.setValue(destination);
    else cmdInput.setValue("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldIndex]);

  useEffect(() => {
    if (field !== "destination" || status === "done") {
      onFooter({ hidden: true });
      return () => onFooter(null);
    }
    onFooter({
      kind: "normal", cmdInput, suggestions: [], suggestionIdx: 0, cmdResult: "",
      statusText: `${chordLabel("enter")} export   ${chordLabel("escape")} cancel`,
    });
    return () => onFooter(null);
  }, [field, status, cmdInput, cmdInput.value, cmdInput.cursor, onFooter]);

  const cycle = (delta: number) => {
    if (field === "scope") {
      const next = Math.max(0, Math.min(SCOPES.length - 1, scopeIndex + delta));
      setScopeIndex(next);
      const suffix = FORMATS[formatIndex] === "markdown" ? "md" : FORMATS[formatIndex];
      setDestination(`.gitgo/exports/${project}-${next === 0 ? "minimal" : "full"}.${suffix}`);
    } else if (field === "format") {
      const next = Math.max(0, Math.min(FORMATS.length - 1, formatIndex + delta));
      setFormatIndex(next);
      const suffix = FORMATS[next] === "markdown" ? "md" : FORMATS[next];
      setDestination(previous => previous.replace(/\.[^.\\/]+$/, `.${suffix}`));
    }
  };

  const submit = () => {
    const outputPath = (field === "destination" ? cmdInput.value : destination).trim();
    if (!outputPath) { setResult("Destination is required"); setStatus("error"); return; }
    setDestination(outputPath);
    setStatus("exporting");
    exportData(client, project, {
      minimal: scopeIndex === 0,
      output_path: outputPath,
      output_format: FORMATS[formatIndex],
    }).then((value: any) => {
      setResult(`${value.path}   ${value.size} bytes   sha256 ${String(value.sha256 || "").slice(0, 12)}`);
      setStatus("done");
    }).catch((error: any) => {
      setResult(String(error?.message || error));
      setStatus("error");
    });
  };

  useInput((input: string, key: any) => {
    for (const action of resolveExportKey(status, field, input, key)) {
      if (action.type === "dismiss") onDismiss();
      else if (action.type === "moveField") {
        if (field === "destination") setDestination(cmdInput.value);
        setFieldIndex(value => Math.max(0, Math.min(FIELDS.length - 1, value + action.delta)));
      } else if (action.type === "cycle") cycle(action.delta);
      else if (action.type === "text") applyTextOp(action.op, cmdInput);
      else if (action.type === "confirm") {
        if (fieldIndex < FIELDS.length - 1) setFieldIndex(value => value + 1);
        else submit();
      }
    }
  });

  const values: Record<Field, string> = {
    scope: SCOPES[scopeIndex],
    format: FORMATS[formatIndex],
    destination: field === "destination" ? "" : destination,
  };

  return <Box flexDirection="column" padding={1} width={cols}>
    <Box marginBottom={1}><Text bold>Export — {project}</Text></Box>
    {FIELDS.map((item, index) => {
      const active = index === fieldIndex;
      const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
      return <Box key={item}>
        <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>
          {(item[0]!.toUpperCase() + item.slice(1)).padEnd(18)}
        </Text>
        <Text dimColor={!active} backgroundColor={style.bg}> {values[item] || " "}</Text>
      </Box>;
    })}
    <Box marginTop={1}>
      <Text
        color={status === "error" ? colors.danger : status === "done" ? colors.success : undefined}
        dimColor={status === "idle"}
      >
        {status === "idle"
          ? `${chordLabel("leftRight")} change   ${chordLabel("upDown")} field   ${chordLabel("enter")} next`
          : status === "exporting" ? "Exporting…" : result}
      </Text>
    </Box>
  </Box>;
});
