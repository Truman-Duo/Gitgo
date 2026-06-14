// test-scroll.tsx — minimal reproduction of tab switch scroll
import React, { useState } from "react";
import { renderSync, Box, Text, useInput } from "./vendor/ink/src/index.js";

function Tall() {
  return (
    <Box flexDirection="column">
      {Array.from({ length: 30 }).map((_, i) => (
        <Text key={i}>Tall line {i + 1}</Text>
      ))}
    </Box>
  );
}

function Short() {
  return (
    <Box flexDirection="column">
      {Array.from({ length: 5 }).map((_, i) => (
        <Text key={i}>Short line {i + 1}</Text>
      ))}
    </Box>
  );
}

function App() {
  const [tab, setTab] = useState(0);
  useInput((_input: string, key: any) => {
    if (key.leftArrow || key.rightArrow) setTab((t) => (t === 0 ? 1 : 0));
    if (key.escape) process.exit(0);
  });
  return (
    <Box flexDirection="column" padding={1}>
      <Text bold>Tab: {tab === 0 ? "TALL (30 lines)" : "SHORT (5 lines)"}</Text>
      <Text dimColor>Press ←→ to switch | Esc to exit</Text>
      {tab === 0 ? <Tall /> : <Short />}
    </Box>
  );
}

const { waitUntilExit } = renderSync(<App />);
waitUntilExit();
