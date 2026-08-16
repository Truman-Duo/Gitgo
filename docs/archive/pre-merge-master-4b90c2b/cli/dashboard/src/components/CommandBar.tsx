// src/components/CommandBar.tsx
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";

export type Suggestion = { label: string; description: string };

type Props = {
  buf: string | null;
  cursor: number;
  result: string;
  cols: number;
  suggestions: Suggestion[];
  suggestionIdx: number;
};

export const CommandBar = memo(function CommandBar({
  buf, cursor, result, cols, suggestions, suggestionIdx,
}: Props) {
  const barWidth = Math.max(60, cols || 80);
  const showSuggestions = buf !== null && suggestions.length > 0;

  if (buf !== null) {
    const before = buf.slice(0, cursor);
    const at = cursor < buf.length ? buf[cursor] : " ";
    const after = buf.slice(cursor + 1);
    const isCommand = buf.startsWith(":");

    return (
      <Box width={barWidth} borderStyle="single" borderColor="green"
        paddingLeft={1} paddingRight={1} flexDirection="column">
        <Box flexDirection="row">
          <Text color="cyan" bold>{"> "}</Text>
          <Text>{before}</Text>
          <Text inverse>{at}</Text>
          <Text>{after}</Text>
          {!isCommand && buf.length > 0 && (
            <Text color="red">  (type : for command)</Text>
          )}
        </Box>
        {showSuggestions && (
          <Box flexDirection="column" paddingLeft={2} marginTop={0}>
            {suggestions.slice(0, 6).map((s, i) => {
              const active = i === suggestionIdx % suggestions.length;
              return (
                <Box key={s.label} flexDirection="row">
                  <Text color={active ? "cyan" : undefined} bold={active}>
                    {active ? "▸ " : "  "}:{s.label}
                  </Text>
                  <Text dimColor>{" ("}{s.description}{")"}</Text>
                </Box>
              );
            })}
          </Box>
        )}
        {result && (
          <Box flexDirection="column">
            <Text color="yellow">{result}</Text>
          </Box>
        )}
      </Box>
    );
  }

  return (
    <Box width={barWidth} borderStyle="single" borderColor="green"
      paddingLeft={1} paddingRight={1} flexDirection="column">
      <Box flexDirection="row">
        <Text dimColor>{"> "}</Text>
        <Text dimColor>: for commands  ↑↓ move focus  h help  q quit</Text>
      </Box>
      {result && (
        <Box flexDirection="column">
          <Text color="yellow">{result}</Text>
        </Box>
      )}
    </Box>
  );
});
