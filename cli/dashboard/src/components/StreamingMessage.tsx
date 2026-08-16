// src/components/StreamingMessage.tsx — shared transient streaming-row renderer.
// Renders the in-flight token stream (gutter + timestamp + live spinner + wrapped
// text + tool cards), kept separate from the persisted message list.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { StreamingRow } from "../types.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import { Spinner } from "./Spinner.js";
import { colors, wrap } from "../theme/index.js";

type Props = {
  streaming: StreamingRow;
  contentWidth: number;
};

export function StreamingMessage({ streaming, contentWidth }: Props) {
  const lines = wrap(streaming.text || "", contentWidth - 4);
  const firstLine = lines[0] || "";
  const restLines = lines.slice(1);

  return (
    <Box flexDirection="column">
      <Box flexDirection="row">
        <Text color={colors.chat.gutter}>● </Text>
        <Text color={colors.warning}><Spinner /> </Text>
        <Text>{firstLine || "..."}</Text>
      </Box>
      {restLines.map((line, j) => (
        <Box key={`stream-${j}`}>
          <Text dimColor>{`    ${line}`}</Text>
        </Box>
      ))}
      {streaming.tools.length > 0 ? (
        <Box flexDirection="column" marginTop={1}>
          {streaming.tools.map((tc, ti) => (
            <Box key={ti}>
              <ToolCallDisplay tool={tc} />
            </Box>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
