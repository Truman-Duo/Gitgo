// src/components/DialogSelect.tsx — Generic fuzzy-search selection panel
// Used by: which-key (? key), command palette (/ key)
// Keyboard handling is done by the parent via overlay system (no internal useInput).

import React, { memo, useState, useMemo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import { useTextInput, applyTextOp } from "../hooks/useTextInput.js";
import { resolveDialogSelectKey } from "../input/overlays/dialogSelect.js";
import { colors, usePanelSize, separator, placeholderChar } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

export type DialogItem = {
  id: string;
  title: string;
  category?: string;
  hint?: string;        // e.g. keyboard shortcut shown on the right
};

type Props = {
  items: DialogItem[];
  onSelect: (id: string) => void;
  onDismiss: () => void;
  placeholder?: string;
  title?: string;
  height: number;
};

export const DialogSelect = memo(function DialogSelect({
  items,
  onSelect,
  onDismiss,
  placeholder = "Type to filter...",
  title,
  height,
}: Props) {
  const query = useTextInput("");
  const { contentH } = usePanelSize({ minHeight: height, headerRows: 5 });

  const filtered = useMemo(() => {
    const q = query.value.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => {
      const haystack = `${item.title} ${item.category || ""} ${item.hint || ""}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [items, query.value]);

  const maxVisible = Math.max(5, contentH);
  const visible = filtered.slice(0, maxVisible);

  // Group by category
  const grouped = useMemo(() => {
    const map = new Map<string, DialogItem[]>();
    for (const item of visible) {
      const cat = item.category || "Other";
      if (!map.has(cat)) map.set(cat, []);
      map.get(cat)!.push(item);
    }
    return [...map.entries()];
  }, [visible]);

  const [selIdx, setSelIdx] = useState(0);

  // Keyboard: own useInput (global handler suppressed when overlay is open)
  useInput((input: string, key: any) => {
    for (const a of resolveDialogSelectKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSelIdx((s) => Math.max(0, Math.min(filtered.length - 1, s + a.delta)));
      } else if (a.type === "confirm") {
        const item = filtered[selIdx];
        if (item) onSelect(item.id);
      } else if (a.type === "text") {
        applyTextOp(a.op, query);
        if (a.op.op === "insert" || a.op.op === "delete_back" || a.op.op === "delete_forward") {
          setSelIdx(0);
        }
      }
    }
  });

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
      {/* Title / search line */}
      <Box flexDirection="row" justifyContent="space-between">
        <Box flexDirection="row" gap={1}>
          {title ? <Text bold color={colors.accent}>{title}</Text> : null}
          <Text dimColor>{placeholder}</Text>
        </Box>
        <Text dimColor>[{chordLabel("escape")}] dismiss</Text>
      </Box>

      {/* Search input */}
      <Box flexDirection="row">
        <Text color={colors.success}>{"> "}</Text>
        <Text>{query.value || " "}</Text>
        {query.value.length === 0 ? <Text dimColor>{placeholderChar(true)}</Text> : null}
      </Box>

      <Text dimColor>{separator(40)}</Text>

      {/* Grouped results */}
      {grouped.length === 0 ? (
        <Box paddingTop={1}>
          <Text dimColor>No matches</Text>
        </Box>
      ) : (
        grouped.map(([category, catItems]) => (
          <Box key={category} flexDirection="column">
            <Text dimColor bold>{category}</Text>
            {catItems.map((item) => (
              <Box key={item.id} flexDirection="row" justifyContent="space-between" paddingLeft={2}>
                <Text>{item.title}</Text>
                {item.hint ? <Text dimColor>{item.hint}</Text> : null}
              </Box>
            ))}
          </Box>
        ))
      )}

      {/* More indicator */}
      {filtered.length > maxVisible ? (
        <Text dimColor>  ... {filtered.length - maxVisible} more results</Text>
      ) : null}

      <Text dimColor>{separator(40)}</Text>
      <Text dimColor>[{chordLabel("enter")}] Select  [{chordLabel("escape")}] Dismiss  Type to filter</Text>
    </Box>
  );
});
