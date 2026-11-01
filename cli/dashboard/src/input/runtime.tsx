import React, { createContext, useContext, useId, useLayoutEffect, useRef, useEffect, useState, useCallback, useMemo } from "react";
import { useInput, useSelection, ScrollBox } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import { InputRouter, applicationBindings, type InputKey, type BindingSettings } from "./router.js";
import { readClipboard } from "../utils/clipboard.js";
import {scrollViewport} from "./scrollViewport.js";

const RouterContext = createContext<InputRouter | null>(null);
const PriorityContext = createContext(0);
const BindingScopeContext = createContext("");
const BindingsContext = createContext({revision: 0, configure: (_settings: BindingSettings) => {}});

export function useBindingSettings(settings: BindingSettings) {
  const {configure} = useContext(BindingsContext);
  useLayoutEffect(() => configure(settings), [configure, settings]);
}

export type VerificationInput = {afterMs: number; input: string; key: InputKey};

export function InputProvider({ children, settings, verificationInput }: {
  children: React.ReactNode; settings?: BindingSettings; verificationInput?: VerificationInput[];
}) {
  const router = useRef(new InputRouter(applicationBindings)).current;
  const [revision, setRevision] = useState(0);
  const configure = useCallback((value: BindingSettings) => {
    router.bindings.configure(value);
    setRevision(n => n + 1);
  }, [router]);
  const bindingsContext = useMemo(() => ({revision, configure}), [revision, configure]);
  const selection = useSelection();
  useLayoutEffect(() => { if (settings) router.bindings.configure(settings); }, [router, settings]);
  useLayoutEffect(() => () => router.bindings.configure({version: 1, overrides: {}}), [router]);
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
  return <RouterContext.Provider value={router}><BindingsContext.Provider value={bindingsContext}>{children}</BindingsContext.Provider></RouterContext.Provider>;
}

export function InputLayer({ priority, bindingContext, children }: { priority: number; bindingContext?: string; children: React.ReactNode }) {
  const inherited = useContext(BindingScopeContext);
  return <PriorityContext.Provider value={priority}><BindingScopeContext.Provider value={bindingContext ?? inherited}>{children}</BindingScopeContext.Provider></PriorityContext.Provider>;
}

/** Compatibility adapter: existing scene resolvers remain pure consumers.
 * Return false to defer to another registered owner; void means consumed.
 */
export function useManagedInput(handler: (input: string, key: InputKey) => void | boolean,
  options: { isActive?: boolean; priority?: number; clipboard?: boolean; bindingContext?: string; raw?: boolean } = {}) {
  const router = useContext(RouterContext);
  useContext(BindingsContext); // Re-render hints together with the effective binding map.
  const layer = useContext(PriorityContext);
  const bindingScope = useContext(BindingScopeContext);
  const id = useId();
  const current = useRef({ handler, options, bindingScope });
  useLayoutEffect(() => { current.current = { handler, options, bindingScope }; });
  useLayoutEffect(() => {
    if (!router) throw new Error("INPUT_ROUTER_MISSING: mount InputProvider");
    let mounted = true;
    const unregister = router.register({ id, priority: layer + (options.priority || 0),
      enabled: () => current.current.options.isActive !== false,
      context: () => current.current.options.bindingContext ?? current.current.bindingScope,
      raw: () => Boolean(current.current.options.raw),
      handle: ({ input, key }) => {
        if (!current.current.options.raw && key.ctrl && input === "v" && current.current.options.clipboard !== false) {
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
    scrollViewport(ref.current, direction * (key.pageUp || key.pageDown ? 10 : 3));
    return true;
  }, { priority: 50, clipboard: false, isActive: enabled });
}

export function ScrollableInputLayer({ children, priority = 100, bindingContext }: { children: React.ReactNode; priority?: number; bindingContext?: string }) {
  return <InputLayer priority={priority} bindingContext={bindingContext}><ScrollableContent>{children}</ScrollableContent></InputLayer>;
}
function ScrollableContent({ children }: { children: React.ReactNode }) {
  const ref = useRef<ScrollBoxHandle>(null);
  useScrollInput(ref);
  return <ScrollBox ref={ref} flexDirection="column" flexGrow={1}>{children}</ScrollBox>;
}

/** Read the live registry used by dispatch; revision changes refresh all consumers. */
export function useEffectiveBindings() {
  useContext(BindingsContext);
  return applicationBindings;
}
export function useBindingRevision() {
  return useContext(BindingsContext).revision;
}
export function useConfigureBindings() {
  return useContext(BindingsContext).configure;
}
