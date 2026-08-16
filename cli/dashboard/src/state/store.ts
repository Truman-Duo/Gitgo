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

export type Scene = "projects" | "workspace" | "process_list" | "agent_detail";

export function isChatScene(scene: Scene): boolean {
  return scene === "workspace" || scene === "agent_detail";
}

export type OverlayType = "help" | "context" | "whichkey" | "dialogSelect" | "quitConfirm" | "createForm" | "configPanel" | "exportPanel" | "statusPanel" | "lessonsPanel" | "governancePanel" | "memoryPanel" | "trialPanel" | "formalPanel" | "runtimeMenu";

export type OverlayEntry = {
  type: OverlayType;
  props?: Record<string, any>;
};

export type Mode = "NORMAL" | "COMMAND";

export type AppState = {
  scene: Scene;
  activeProject: string | null;
  activeAgentId: string | null; // B-level agent process_id for detail view
  processListSelIdx: number;   // process list selection index
  runningBSelIdx: number;      // running B footer strip selection index
  statusBarFocused: boolean;   // workspace-only: running-B strip is in selection mode

  // Mode system — text buffers now managed by useTextInput hook
  mode: Mode;

  // Overview state (from old App.tsx)
  sel: number;
  cmdResult: string;
  overlayStack: OverlayEntry[];  // unified overlay stack (help/context/configPanel/whichkey/dialogSelect)
  cmdHistory: string[];
  cmdHistoryIdx: number;
  suggestionIdx: number;
  refreshKey: number;

  // Chat state (Scene 2)
  chatInputFocused: boolean;
};

// ── App actions (reducer-driven state transitions) ─────────

export type NavigatePatch = Partial<
  Pick<AppState, "activeProject" | "activeAgentId" | "processListSelIdx">
>;

export type AppAction =
  | { type: "navigate"; scene: Scene; patch?: NavigatePatch }
  | { type: "select_project"; index: number }
  | { type: "select_process"; index: number }
  | { type: "select_running_b"; index: number }
  | { type: "push_overlay"; overlay: OverlayType; props?: Record<string, any> }
  | { type: "pop_overlay" }
  | { type: "enter_command" }
  | { type: "exit_command" }
  | { type: "set_chat_input_focused"; focused: boolean }
  | { type: "set_status_bar_focused"; focused: boolean }
  | { type: "set_cmd_result"; text: string }
  | { type: "set_suggestion_idx"; index: number }
  | { type: "push_cmd_history"; cmd: string }
  | { type: "set_cmd_history_idx"; index: number }
  | { type: "set_active_project"; name: string | null }
  | { type: "bump_refresh_key" };

export function reducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case "navigate":
      return {
        ...state,
        scene: action.scene,
        mode: isChatScene(action.scene) ? "NORMAL" : "COMMAND",
        statusBarFocused: false,
        cmdResult: "",
        ...(action.patch ?? {}),
      };
    case "select_project":
      return { ...state, sel: action.index };
    case "select_process":
      return { ...state, processListSelIdx: action.index };
    case "select_running_b":
      return { ...state, runningBSelIdx: action.index };
    case "push_overlay":
      return {
        ...state,
        mode: "NORMAL",
        overlayStack: [...state.overlayStack, { type: action.overlay, props: action.props }],
      };
    case "pop_overlay": {
      const overlayStack = state.overlayStack.slice(0, -1);
      return {
        ...state,
        overlayStack,
        mode:
          overlayStack.length > 0
            ? "NORMAL"
            : isChatScene(state.scene)
            ? "NORMAL"
            : "COMMAND",
      };
    }
    case "enter_command":
      return { ...state, mode: "COMMAND", cmdResult: "", suggestionIdx: 0 };
    case "exit_command":
      return { ...state, mode: "NORMAL", suggestionIdx: 0 };
    case "set_chat_input_focused":
      return { ...state, chatInputFocused: action.focused, statusBarFocused: action.focused ? false : state.statusBarFocused };
    case "set_status_bar_focused":
      return { ...state, statusBarFocused: action.focused, chatInputFocused: action.focused ? false : state.chatInputFocused };
    case "set_cmd_result":
      return { ...state, cmdResult: action.text };
    case "set_suggestion_idx":
      return { ...state, suggestionIdx: action.index };
    case "push_cmd_history":
      return {
        ...state,
        cmdHistory: [...state.cmdHistory, action.cmd],
        cmdHistoryIdx: -1,
      };
    case "set_cmd_history_idx":
      return { ...state, cmdHistoryIdx: action.index };
    case "set_active_project":
      return { ...state, activeProject: action.name };
    case "bump_refresh_key":
      return { ...state, refreshKey: state.refreshKey + 1 };
  }
}

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

export function createReducerStore<T, A>(
  reducer: (state: T, action: A) => T,
  initial: T,
): Store<T> & { dispatch: (action: A) => void } {
  const store = createStore(initial);
  return {
    ...store,
    dispatch(action: A) {
      store.setState((prev) => reducer(prev, action));
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
    activeProject: null,
    activeAgentId: null,
    processListSelIdx: 0,
    runningBSelIdx: 0,
    statusBarFocused: false,

    mode: "COMMAND",

    sel: 0,
    cmdResult: "",
    overlayStack: [],
    cmdHistory: [],
    cmdHistoryIdx: -1,
    suggestionIdx: 0,
    refreshKey: 0,

    chatInputFocused: false,
  };
}
