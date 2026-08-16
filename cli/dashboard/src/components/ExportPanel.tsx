// src/components/ExportPanel.tsx — /export: knowledge export with scope selection
// Color block toggle: Minimal / Full. Enter to export, show result.
import React, { memo, useState } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { resolveExportKey } from "../input/overlayKeymaps.js";
import { colors, useSelectionStyle } from "../theme/index.js";
import { exportData } from "../mcp/tools.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};

const SCOPES = [
  { label: "Minimal", desc: "Lessons + Contract", minimal: true },
  { label: "Full", desc: "Lessons + Contract + Identity + History", minimal: false },
];

export const ExportPanel = memo(function ExportPanel({ client, project, cols, onDismiss }: Props) {
  const [sel, setSel] = useState(0);
  const [status, setStatus] = useState<"idle" | "exporting" | "done" | "error">("idle");
  const [result, setResult] = useState("");

  useInput((input: string, key: any) => {
    for (const a of resolveExportKey(status, input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSel((s) => Math.max(0, Math.min(SCOPES.length - 1, s + a.delta)));
      } else if (a.type === "confirm") {
        const scope = SCOPES[sel];
        if (!scope) continue;
        setStatus("exporting");
        exportData(client, project, scope)
          .then((r: any) => {
            setResult(typeof r === "string" ? r : JSON.stringify(r, null, 2));
            setStatus("done");
          })
          .catch((e: any) => {
            setResult(String(e.message || e));
            setStatus("error");
          });
      }
    }
  });

  return (
    <Box flexDirection="column" padding={1} width={cols}>
      <Box marginBottom={1}>
        <Text bold>Export: {project}</Text>
        <Text dimColor>    {chordLabel("escape")} cancel</Text>
      </Box>

      {status === "exporting" && (
        <Box marginBottom={1}>
          <Text dimColor>Exporting...</Text>
        </Box>
      )}

      {(status === "done" || status === "error") && (
        <Box flexDirection="column" marginBottom={1}>
          <Text color={status === "error" ? colors.danger : colors.success} bold>
            {status === "done" ? "Export complete" : "Export failed"}
          </Text>
          <Text dimColor>{result.slice(0, 200)}</Text>
          <Box marginTop={1}>
            <Text dimColor>Press any key to dismiss</Text>
          </Box>
        </Box>
      )}

      {status === "idle" && (
        <>
          {SCOPES.map((scope, i) => {
            const active = i === sel;
            const scopeStyle = useSelectionStyle(active ? "focused" : "non-focused", "row");
            return (
              <Box key={scope.label} flexDirection="column" marginBottom={1}>
                <Box>
                  <Text
                    color={scopeStyle.fg}
                    backgroundColor={scopeStyle.bg}
                    bold={scopeStyle.bold}
                  >
                    {scope.label}
                  </Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor={!active}>{scope.desc}</Text>
                </Box>
              </Box>
            );
          })}
          <Box marginTop={1}>
            <Text dimColor>{chordLabel("upDown")} select scope    {chordLabel("enter")} export    {chordLabel("escape")} cancel</Text>
          </Box>
        </>
      )}
    </Box>
  );
});
