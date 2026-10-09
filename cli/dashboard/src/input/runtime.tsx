import React, { createContext, useContext, useId, useLayoutEffect, useRef, useEffect } from "react";
import { useInput, useSelection, ScrollBox } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import { InputRouter, applicationBindings, type InputKey, type BindingSettings } from "./router.js";
import { readClipboard } from "../utils/clipboard.js";

const RouterContext = createContext<InputRouter | null>(null);
const PriorityContext = createContext(0);

export type VerificationInput = {afterMs: number; input: string; key: InputKey};

export function InputProvider({ children, settings, verificationInput }: {
  children: React.ReactNode; settings?: BindingSettings; verificationInput?: VerificationInput[];
}) {
  const router = useRef(new InputRouter(applicationBindings)).current;
  const selection = useSelection();
  useLayoutEffect(() => { if (settings) router.bindings.configure(settings); }, [router, settings]);
  // Explicit acceptance flags automate keys through the production router.
  // They never replace the renderer, scene, backend calls or approval policy.
  useEffect(() => {
    const timers = (verificationInput || []).map(event => setTimeout(() => {
      router.dispatch({input: event.input, key: event.key});
    }, event.afterMs));
    return () => timers.forEach(clearTimeout);
  }, [router, verificationInput]);
  useLayoutEffect(() => router.register({ id: "clipboard.copy", priority: 10000, enabled: () => true,
    handle: ({ input, key }) => {
      if (!key.ctrl || input !== "c") return false;
      selection.copySelectionNoClear();
      return true; // Never let a clipboard shortcut reach task cancellation.
    } }), [router, selection]);
  useInput((input, key, event: any) => {
    router.dispatch({
      input,
      key: {...key, paste: Boolean(event?.keypress?.isPasted || (key as InputKey).paste)},
    });
  });
  return <RouterContext.Provider value={router}>{children}</RouterContext.Provider>;
}

export function InputLayer({ priority, children }: { priority: number; children: React.ReactNode }) {
  return <PriorityContext.Provider value={priority}>{children}</PriorityContext.Provider>;
}

/** Compatibility adapter: existing scene resolvers remain pure consumers.
 * Return false to defer to another registered owner; void means consumed.
 */
export function useManagedInput(handler: (input: string, key: InputKey) => void | boolean,
  options: { isActive?: boolean; priority?: number; clipboard?: boolean } = {}) {
  const router = useContext(RouterContext);
  const layer = useContext(PriorityContext);
  const id = useId();
  const current = useRef({ handler, options });
  useLayoutEffect(() => { current.current = { handler, options }; });
  useLayoutEffect(() => {
    if (!router) throw new Error("INPUT_ROUTER_MISSING: mount InputProvider");
    let mounted = true;
    const unregister = router.register({ id, priority: layer + (options.priority || 0),
      enabled: () => current.current.options.isActive !== false,
      handle: ({ input, key }) => {
        if (key.ctrl && input === "v" && current.current.options.clipboard !== false) {
          // All editors receive literal text through their normal input path.
          // Capture the owner/editor callback before asynchronous OS clipboard IO.
          const owner = current.current.handler;
          void readClipboard().then(text => {
            if (mounted && current.current.options.isActive !== false && text) owner(text, {paste: true});
          }).catch(() => undefined);
          return true;
        }
        return current.current.handler(input, key) !== false;
      } });
    return () => { mounted = false; unregister(); };
  }, [router, id, layer, options.priority]);
}

export function useScrollInput(ref: React.RefObject<ScrollBoxHandle | null>, enabled = true) {
  useManagedInput((input, key) => {
    const direction = key.pageUp || key.wheelUp ? -1
      : key.pageDown || key.wheelDown ? 1 : 0;
    if (!direction || !ref.current) return false;
    ref.current.scrollBy(direction * (key.pageUp || key.pageDown ? 10 : 3));
    return true;
  }, { priority: 50, clipboard: false, isActive: enabled });
}

export function ScrollableInputLayer({ children, priority = 100 }: { children: React.ReactNode; priority?: number }) {
  return <InputLayer priority={priority}><ScrollableContent>{children}</ScrollableContent></InputLayer>;
}
function ScrollableContent({ children }: { children: React.ReactNode }) {
  const ref = useRef<ScrollBoxHandle>(null);
  useScrollInput(ref);
  return <ScrollBox ref={ref} flexDirection="column" flexGrow={1}>{children}</ScrollBox>;
}
