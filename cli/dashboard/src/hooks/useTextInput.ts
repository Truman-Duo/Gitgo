// src/hooks/useTextInput.ts — Emacs-level text editing hook
// Provides cursor movement, word navigation, kill-ring, and yank.
// Kill-ring is module-level (max 10 entries), not React state.
//
// v9: Single {value, cursor} state object so callbacks read both atomically
// from prev — all function references are stable ([] deps). Return value
// wrapped in useMemo so React.memo on CommandBar actually works.

import { useState, useCallback, useMemo } from "react";
import type { TextOp } from "../input/keymap.js";

const KILL_RING: string[] = [];
const MAX_KILL = 10;

function pushKill(text: string) {
  if (!text) return;
  KILL_RING.unshift(text);
  if (KILL_RING.length > MAX_KILL) KILL_RING.pop();
}

export type UseTextInputReturn = {
  value: string;
  cursor: number;
  insert: (char: string) => void;
  insertText: (text: string) => void;
  deleteBack: () => void;
  deleteForward: () => void;
  moveCursor: (delta: number) => void;
  moveWord: (delta: number) => void;
  moveToStart: () => void;
  moveToEnd: () => void;
  killToEnd: () => void;
  killToStart: () => void;
  killWordBack: () => void;
  yank: () => void;
  setValue: (text: string) => void;
};

type State = { value: string; cursor: number };

/** Apply a TextOp (from input/keymap.ts) to a useTextInput buffer. */
export function applyTextOp(op: TextOp, buf: UseTextInputReturn): void {
  switch (op.op) {
    case "insert": buf.insertText(op.text); break;
    case "delete_back": buf.deleteBack(); break;
    case "delete_forward": buf.deleteForward(); break;
    case "move_cursor": buf.moveCursor(op.delta); break;
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

  const insert = useCallback((char: string) => {
    if (!char || char.length === 0) return;
    setState((prev) => ({
      value: prev.value.slice(0, prev.cursor) + char + prev.value.slice(prev.cursor),
      cursor: prev.cursor + char.length,
    }));
  }, []);

  const insertText = useCallback((text: string) => {
    if (!text) return;
    setState((prev) => ({
      value: prev.value.slice(0, prev.cursor) + text + prev.value.slice(prev.cursor),
      cursor: prev.cursor + text.length,
    }));
  }, []);

  const deleteBack = useCallback(() => {
    setState((prev) => {
      if (prev.cursor <= 0) return prev;
      return {
        value: prev.value.slice(0, prev.cursor - 1) + prev.value.slice(prev.cursor),
        cursor: prev.cursor - 1,
      };
    });
  }, []);

  const deleteForward = useCallback(() => {
    setState((prev) => {
      if (prev.cursor >= prev.value.length) return prev;
      return {
        value: prev.value.slice(0, prev.cursor) + prev.value.slice(prev.cursor + 1),
        cursor: prev.cursor,
      };
    });
  }, []);

  const moveCursor = useCallback((delta: number) => {
    setState((prev) => ({
      ...prev,
      cursor: Math.max(0, Math.min(prev.value.length, prev.cursor + delta)),
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
    const folded = text.length > 500 ? text.slice(0, 500) + "..." : text;
    setState({ value: folded, cursor: folded.length });
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
      moveWord,
      moveToStart,
      moveToEnd,
      killToEnd,
      killToStart,
      killWordBack,
      yank,
      setValue,
    }),
    [state.value, safeCursor, insert, insertText, deleteBack, deleteForward,
     moveCursor, moveWord, moveToStart, moveToEnd, killToEnd, killToStart,
     killWordBack, yank, setValue],
  );
}
