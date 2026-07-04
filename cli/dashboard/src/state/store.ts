// src/state/store.ts — lightweight createStore + useSyncExternalStore
// Borrowed from Claude Code (src/state/store.ts, 35 lines)
// Provides a minimal reactive store without Redux/Zustand dependencies.

import { useCallback, useRef, useSyncExternalStore } from "react";

// ── Types ──────────────────────────────────────────────────

export type Listener = () => void;

export type Store<T> = {
  getState: () => T;
  setState: (updater: (prev: T) => T) => void;
  subscribe: (listener: Listener) => () => void;
};

export type Scene = "projects" | "workspace" | "agent_detail" | "llm_config";

export type AppState = {
  scene: Scene;
  previousScene: Scene; // for llm_config to know where to go back to
  activeProject: string | null;
  activeAgentId: string | null; // B-level agent process_id for detail view

  // Overview state (from old App.tsx)
  sel: number;
  focus: "table" | "command";
  cmdBuf: string;
  cmdCursor: number;
  cmdResult: string;
  showHelp: boolean;
  cmdHistory: string[];
  cmdHistoryIdx: number;
  suggestionIdx: number;
  refreshKey: number;

  // Chat state (Scene 2)
  chatMessages: { role: string; content: string; timestamp: string }[];
  chatInputFocused: boolean;
};

// ── Factory ────────────────────────────────────────────────

export function createStore<T>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<Listener>();

  return {
    getState: () => state,

    setState(updater: (prev: T) => T) {
      state = updater(state);
      for (const fn of listeners) fn();
    },

    subscribe(listener: Listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

// ── Hook ───────────────────────────────────────────────────

export function useStore<T, R>(
  store: Store<T>,
  selector: (state: T) => R,
): R {
  const prevRef = useRef<R>(selector(store.getState()));

  const subscribe = useCallback(
    (notify: () => void) => {
      const check = () => {
        const next = selector(store.getState());
        if (!Object.is(next, prevRef.current)) {
          prevRef.current = next;
          notify();
        }
      };
      return store.subscribe(check);
    },
    [store, selector],
  );

  return useSyncExternalStore(
    subscribe,
    () => selector(store.getState()),
  );
}

// ── Default state ──────────────────────────────────────────

export function initialAppState(): AppState {
  return {
    scene: "projects",
    previousScene: "projects",
    activeProject: null,
    activeAgentId: null,

    sel: 0,
    focus: "table",
    cmdBuf: "",
    cmdCursor: 0,
    cmdResult: "",
    showHelp: false,
    cmdHistory: [],
    cmdHistoryIdx: -1,
    suggestionIdx: 0,
    refreshKey: 0,

    chatMessages: [],
    chatInputFocused: false,
  };
}
