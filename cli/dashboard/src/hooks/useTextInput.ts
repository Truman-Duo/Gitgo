// src/hooks/useTextInput.ts — Emacs-level text editing hook
// Provides cursor movement, word navigation, kill-ring, and yank.
// Kill-ring is module-level (max 10 entries), not React state.
//
// v9: Single {value, cursor} state object so callbacks read both atomically
// from prev — all function references are stable ([] deps). Return value
// wrapped in useMemo so React.memo on CommandBar actually works.

import { useState, useCallback, useMemo, useRef } from "react";
import { stringWidth } from "@anthropic/ink";
import type { TextOp } from "../input/keymap.js";
import { PromptPasteStore } from "../input/pasteStore.js";

const KILL_RING: string[] = [];
const MAX_KILL = 10;

function pushKill(text: string) {
  if (!text) return;
  KILL_RING.unshift(text);
  if (KILL_RING.length > MAX_KILL) KILL_RING.pop();
}

export function visualLineCursor(value: string, cursor: number, delta: -1 | 1, rawWidth: number): number {
  const width = Math.max(1, Math.floor(rawWidth));
  const points: Array<{index: number; row: number; column: number}> = [];
  const Segmenter = (Intl as any).Segmenter;
  const segments: Array<{segment: string; index: number}> = Segmenter
    ? Array.from(new Segmenter(undefined, {granularity: "grapheme"}).segment(value))
    : (() => {
        let offset = 0;
        return Array.from(value).map(segment => {
          const item = {segment, index: offset};
          offset += segment.length;
          return item;
        });
      })();
  let row = 0;
  let column = 0;
  points.push({index: 0, row, column});
  for (const item of segments) {
    if (item.segment.includes("\n")) {
      row += 1;
      column = 0;
    } else if (item.segment === "\r") {
      // Be defensive for values restored from pre-normalization sessions.
      // A carriage return consumes no terminal cell of its own.
    } else {
      const cellWidth = Math.max(0, stringWidth(item.segment));
      if (column > 0 && column + cellWidth > width) {
        row += 1;
        column = 0;
      }
      column += cellWidth;
    }
    points.push({index: item.index + item.segment.length, row, column});
  }
  const current = points.reduce((best, point) =>
    Math.abs(point.index - cursor) < Math.abs(best.index - cursor) ? point : best,
  points[0]!);
  const candidates = points.filter(point => point.row === current.row + delta);
  if (candidates.length === 0) return cursor;
  return candidates.reduce((best, point) =>
    Math.abs(point.column - current.column) < Math.abs(best.column - current.column) ? point : best,
  candidates[0]!).index;
}

export type UseTextInputReturn = {
  value: string;
  cursor: number;
  insert: (char: string) => void;
  insertText: (text: string, options?: { paste?: boolean }) => void;
  deleteBack: () => void;
  deleteForward: () => void;
  moveCursor: (delta: number) => void;
  moveVisualLine: (delta: -1 | 1, width: number) => void;
  moveWord: (delta: number) => void;
  moveToStart: () => void;
  moveToEnd: () => void;
  killToEnd: () => void;
  killToStart: () => void;
  killWordBack: () => void;
  yank: () => void;
  setValue: (text: string) => void;
  materialize: (displayValue?: string) => string;
  setExternalValue: (text: string) => void;
};

type State = { value: string; cursor: number };

/** Apply a TextOp (from input/keymap.ts) to a useTextInput buffer. */
export function applyTextOp(op: TextOp, buf: UseTextInputReturn): void {
  switch (op.op) {
    case "insert": buf.insertText(op.text, {paste: op.paste}); break;
    case "delete_back": buf.deleteBack(); break;
    case "delete_forward": buf.deleteForward(); break;
    case "move_cursor": buf.moveCursor(op.delta); break;
    case "move_visual_line": buf.moveVisualLine(op.delta, op.width); break;
    case "move_word": buf.moveWord(op.delta); break;
    case "move_to_start": buf.moveToStart(); break;
    case "move_to_end": buf.moveToEnd(); break;
    case "kill_to_end": buf.killToEnd(); break;
    case "kill_to_start": buf.killToStart(); break;
    case "kill_word_back": buf.killWordBack(); break;
    case "yank": buf.yank(); break;
    case "set_value": buf.setValue(op.text); break;
  }
}

export function useTextInput(initialValue = ""): UseTextInputReturn {
  const [state, setState] = useState<State>({ value: initialValue, cursor: 0 });
  const pastes = useRef(new PromptPasteStore());

  const boundaries = useCallback((value: string): number[] => {
    const Segmenter = (Intl as any).Segmenter;
    if (Segmenter) {
      const result = [0];
      for (const item of new Segmenter(undefined, {granularity: "grapheme"}).segment(value)) {
        const end = Number(item.index) + String(item.segment).length;
        if (end > result[result.length - 1]!) result.push(end);
      }
      return result;
    }
    const result = [0];
    let offset = 0;
    for (const char of Array.from(value)) { offset += char.length; result.push(offset); }
    return result;
  }, []);

  const previousBoundary = useCallback((value: string, cursor: number) => {
    const points = boundaries(value);
    for (let index = points.length - 1; index >= 0; index--) {
      if (points[index]! < cursor) return points[index]!;
    }
    return 0;
  }, [boundaries]);

  const nextBoundary = useCallback((value: string, cursor: number) => {
    for (const point of boundaries(value)) if (point > cursor) return point;
    return value.length;
  }, [boundaries]);

  const insert = useCallback((char: string) => {
    if (!char || char.length === 0) return;
    setState((prev) => ({
      value: prev.value.slice(0, prev.cursor) + char + prev.value.slice(prev.cursor),
      cursor: prev.cursor + char.length,
    }));
  }, []);

  const insertText = useCallback((text: string, options: {paste?: boolean} = {}) => {
    if (!text) return;
    const visible = pastes.current.collapse(text, Boolean(options.paste));
    setState((prev) => ({
      value: prev.value.slice(0, prev.cursor) + visible + prev.value.slice(prev.cursor),
      cursor: prev.cursor + visible.length,
    }));
  }, []);

  const deleteBack = useCallback(() => {
    setState((prev) => {
      if (prev.cursor <= 0) return prev;
      const start = previousBoundary(prev.value, prev.cursor);
      return { value: prev.value.slice(0, start) + prev.value.slice(prev.cursor), cursor: start };
    });
  }, [previousBoundary]);

  const deleteForward = useCallback(() => {
    setState((prev) => {
      if (prev.cursor >= prev.value.length) return prev;
      const end = nextBoundary(prev.value, prev.cursor);
      return { value: prev.value.slice(0, prev.cursor) + prev.value.slice(end), cursor: prev.cursor };
    });
  }, [nextBoundary]);

  const moveCursor = useCallback((delta: number) => {
    setState((prev) => ({
      ...prev,
      cursor: delta < 0
        ? previousBoundary(prev.value, prev.cursor)
        : delta > 0 ? nextBoundary(prev.value, prev.cursor) : prev.cursor,
    }));
  }, [previousBoundary, nextBoundary]);

  const moveVisualLine = useCallback((delta: -1 | 1, rawWidth: number) => {
    setState((prev) => ({
      ...prev,
      cursor: visualLineCursor(prev.value, prev.cursor, delta, rawWidth),
    }));
  }, []);

  const moveWord = useCallback((delta: number) => {
    setState((prev) => {
      const wordRe = /[\w一-鿿]+|[^\w\s一-鿿]+/g;
      const boundaries: number[] = [0, prev.value.length];
      let m: RegExpExecArray | null;
      while ((m = wordRe.exec(prev.value)) !== null) {
        boundaries.push(m.index, m.index + m[0].length);
      }
      boundaries.sort((a, b) => a - b);
      const uniq = boundaries.filter((v, i, a) => a.indexOf(v) === i);

      let idx = uniq.indexOf(prev.cursor);
      if (idx === -1) {
        for (let i = 0; i < uniq.length; i++) {
          if (uniq[i] > prev.cursor) { idx = i; break; }
        }
        if (idx === -1) idx = uniq.length;
      }
      const next = Math.max(0, Math.min(uniq.length - 1, idx + delta));
      return { ...prev, cursor: uniq[next] };
    });
  }, []);

  const moveToStart = useCallback(() => {
    setState((prev) => ({ ...prev, cursor: 0 }));
  }, []);

  const moveToEnd = useCallback(() => {
    setState((prev) => ({ ...prev, cursor: prev.value.length }));
  }, []);

  const killToEnd = useCallback(() => {
    setState((prev) => {
      if (prev.cursor >= prev.value.length) return prev;
      pushKill(prev.value.slice(prev.cursor));
      return { value: prev.value.slice(0, prev.cursor), cursor: prev.cursor };
    });
  }, []);

  const killToStart = useCallback(() => {
    setState((prev) => {
      if (prev.cursor <= 0) return prev;
      pushKill(prev.value.slice(0, prev.cursor));
      return { value: prev.value.slice(prev.cursor), cursor: 0 };
    });
  }, []);

  const killWordBack = useCallback(() => {
    setState((prev) => {
      if (prev.cursor <= 0) return prev;
      let i = prev.cursor - 1;
      while (i > 0 && prev.value[i - 1] !== " " && prev.value[i - 1] !== "\n") i--;
      const killed = prev.value.slice(i, prev.cursor);
      pushKill(killed);
      return {
        value: prev.value.slice(0, i) + prev.value.slice(prev.cursor),
        cursor: i,
      };
    });
  }, []);

  const yank = useCallback(() => {
    if (KILL_RING.length === 0) return;
    const text = KILL_RING[0];
    setState((prev) => ({
      value: prev.value.slice(0, prev.cursor) + text + prev.value.slice(prev.cursor),
      cursor: prev.cursor + text.length,
    }));
  }, []);

  const setValue = useCallback((text: string) => {
    if (!text) pastes.current.clear();
    setState({ value: text, cursor: text.length });
  }, []);

  const materialize = useCallback((displayValue = state.value) => pastes.current.materialize(displayValue), [state.value]);

  const setExternalValue = useCallback((text: string) => {
    // The external editor is itself the large-text editing surface.  Its
    // result is deliberate input, not a terminal paste burst, so replacing it
    // with an opaque [Pasted text] token makes a successful import look lost
    // and prevents the user from reviewing the draft in the bar.
    pastes.current.clear();
    const normalized = text.replace(/\r\n?/g, "\n");
    setState({value: normalized, cursor: normalized.length});
  }, []);

  const safeCursor = Math.max(0, Math.min(state.value.length, state.cursor));

  return useMemo<UseTextInputReturn>(
    () => ({
      value: state.value,
      cursor: safeCursor,
      insert,
      insertText,
      deleteBack,
      deleteForward,
      moveCursor,
      moveVisualLine,
      moveWord,
      moveToStart,
      moveToEnd,
      killToEnd,
      killToStart,
      killWordBack,
      yank,
      setValue,
      materialize,
      setExternalValue,
    }),
    [state.value, safeCursor, insert, insertText, deleteBack, deleteForward,
     moveCursor, moveVisualLine, moveWord, moveToStart, moveToEnd, killToEnd, killToStart,
     killWordBack, yank, setValue, materialize, setExternalValue],
  );
}
