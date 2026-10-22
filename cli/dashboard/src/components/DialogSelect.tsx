// src/components/DialogSelect.tsx — Generic fuzzy-search selection panel
// Used by: which-key (? key), command palette (/ key)
// Owns keyboard input while the parent suppresses global overlay shortcuts.

import React, { memo, useState, useMemo } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import { useTextInput, applyTextOp } from "../hooks/useTextInput.js";
import { resolveDialogSelectKey } from "../input/overlays/dialogSelect.js";
import { colors, usePanelSize, separator, placeholderChar, useSelectionStyle } from "../theme/index.js";
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
  const { contentH, w } = usePanelSize({ minHeight: height, headerRows: 8, widthOffset: 2 });
  const [selIdx, setSelIdx] = useState(0);

  const filtered = useMemo(() => {
    const q = query.value.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => {
      const haystack = `${item.title} ${item.category || ""} ${item.hint || ""}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [items, query.value]);

  const activeIndex = Math.min(selIdx, Math.max(0, filtered.length - 1));
  let maxVisible = Math.max(1, contentH - 1);
  const windowStart = () => Math.max(0, Math.min(activeIndex - Math.floor((maxVisible - 1) / 2), filtered.length - maxVisible));
  let start = windowStart();
  let visible = filtered.slice(start, start + maxVisible);
  // Category headings consume terminal rows as well as the result items.
  while (visible.length > 1 && visible.length + new Set(visible.map(item => item.category || "Other")).size > contentH) {
    maxVisible -= 1; start = windowStart();
    visible = filtered.slice(start, start + maxVisible);
  }

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

  // Keyboard: own useInput (global handler suppressed when overlay is open)
  useInput((input: string, key: any) => {
    for (const a of resolveDialogSelectKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSelIdx((s) => Math.max(0, Math.min(filtered.length - 1, s + a.delta)));
      } else if (a.type === "confirm") {
        const item = filtered[activeIndex];
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

      <Text dimColor>{separator(w)}</Text>

      {/* Grouped results */}
      {grouped.length === 0 ? (
        <Box paddingTop={1}>
          <Text dimColor>No matches</Text>
        </Box>
      ) : (
        grouped.map(([category, catItems]) => (
          <Box key={category} flexDirection="column">
            {contentH > 1 ? <Text dimColor bold>{category}</Text> : null}
            {catItems.map((item) => {
              const selected = item.id === filtered[activeIndex]?.id;
              const style = useSelectionStyle(selected ? "focused" : "non-focused", "row");
              return <Box key={item.id} flexDirection="row" justifyContent="space-between" paddingLeft={2} backgroundColor={style.bg}>
                <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>{item.title}</Text>
                {item.hint ? <Text dimColor>{item.hint}</Text> : null}
              </Box>;
            })}
          </Box>
        ))
      )}

      {/* More indicator */}
      {filtered.length > visible.length ? (
        <Text dimColor>  ... {filtered.length - visible.length} more results</Text>
      ) : null}

      <Text dimColor>{separator(w)}</Text>
      <Text dimColor>[{chordLabel("enter")}] Select  [{chordLabel("escape")}] Dismiss  Type to filter</Text>
    </Box>
  );
});
