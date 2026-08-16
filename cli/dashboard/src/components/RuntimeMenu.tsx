// src/components/RuntimeMenu.tsx — /runtime secondary menu (LLMConfig-style tab header)
// Tab header: only the active tab gets a background; inactive tabs are transparent.
// (The dim "detail" background appears only when a tab is expanded internally,
//  which this menu does not yet have.)
import React, { memo, useState } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import { resolveRuntimeMenuKey } from "../input/overlayKeymaps.js";
import { colors, usePanelSize, separator } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type RuntimeItem = { id: string; label: string; title: string; desc: string; subs?: string[] };

const ITEMS: RuntimeItem[] = [
  { id: "lesson", label: "lesson", title: "Lessons", desc: "Runtime lessons and rules", subs: ["list", "search", "verify"] },
  { id: "contract", label: "contract", title: "Contract", desc: "Project contract" },
  { id: "governance", label: "governance", title: "Governance", desc: "Quality metrics, change patterns, event feed, releases", subs: ["quality", "patterns", "feed", "releases"] },
  { id: "memory", label: "memory", title: "Memory Snapshots", desc: "Create, list, restore snapshots", subs: ["snapshot", "list", "restore"] },
  { id: "history", label: "history", title: "Operation History", desc: "Full operation history", subs: ["full"] },
  { id: "context", label: "context", title: "Context Panel", desc: "Live context window" },
  { id: "trial", label: "trial", title: "Trial (External PRs)", desc: "Incoming PR triage", subs: ["list", "triage"] },
  { id: "formal", label: "formal", title: "Formal Commits", desc: "Formal commit management", subs: ["list", "edit", "delete", "dissolve"] },
];

type Props = {
  cols: number;
  rows: number;
  onSelect: (subCmd: string) => void;
  onDismiss: () => void;
};

export const RuntimeMenu = memo(function RuntimeMenu({ cols: _cols, rows: _rows, onSelect, onDismiss }: Props) {
  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });
  const [selIdx, setSelIdx] = useState(0);

  useInput((input: string, key: any) => {
    for (const a of resolveRuntimeMenuKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSelIdx((s) => (s + a.delta + ITEMS.length) % ITEMS.length);
      } else if (a.type === "confirm") {
        const item = ITEMS[selIdx];
        if (item) onSelect(item.id);
      }
    }
  });

  const selected = ITEMS[selIdx]!;

  return (
    <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
      {/* Tab header */}
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-evenly">
          {ITEMS.map((item, i) => {
            const active = i === selIdx;
            const bg = active ? colors.tab.active.bg : undefined;
            const fg = active ? colors.tab.active.fg : undefined;
            return (
              <Box key={item.id} backgroundColor={bg} paddingLeft={1} paddingRight={1}>
                <Text color={fg} backgroundColor={bg} bold={active} dimColor={!active}>
                  {item.label}
                </Text>
              </Box>
            );
          })}
        </Box>
        <Text color={colors.divider.color}>{separator(w)}</Text>
      </Box>

      {/* Active tab content */}
      <Box flexDirection="column" flexGrow={1} paddingTop={1}>
        <Box marginBottom={1}>
          <Text bold>{selected.title}</Text>
        </Box>
        <Text dimColor>{selected.desc}</Text>
        {selected.subs && selected.subs.length > 0 ? (
          <Box marginTop={1}>
            <Text dimColor>sub: {selected.subs.join("  ")}</Text>
          </Box>
        ) : null}
        <Box flexGrow={1} />
        <Text dimColor>{chordLabel("leftRight")} switch    {chordLabel("enter")} open    {chordLabel("escape")} dismiss</Text>
      </Box>
    </Box>
  );
});
