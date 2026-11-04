import React, { createContext, useContext, useId, useLayoutEffect, useRef, useEffect, useState, useCallback, useMemo } from "react";
import { useInput, useSelection, ScrollBox } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import { InputRouter, applicationBindings, type InputKey, type BindingSettings } from "./router.js";
import { readClipboard } from "../utils/clipboard.js";
import {scrollViewport} from "./scrollViewport.js";

const RouterContext = createContext<InputRouter | null>(null);
const PriorityContext = createContext(0);
const InputEnabledContext = createContext(true);
const BindingScopeContext = createContext("");
const BindingsContext = createContext({revision: 0, configure: (_settings: BindingSettings) => {}});
const ClipboardReaderContext = createContext(readClipboard);

export function useBindingSettings(settings: BindingSettings) {
  const {configure} = useContext(BindingsContext);
  useLayoutEffect(() => configure(settings), [configure, settings]);
}

export type VerificationInput = {afterMs: number; input: string; key: InputKey};

export function InputProvider({ children, settings, verificationInput, clipboardReader=readClipboard }: {
  children: React.ReactNode; settings?: BindingSettings; verificationInput?: VerificationInput[];
  clipboardReader?:()=>Promise<string>;
}) {
  const router = useRef(new InputRouter(applicationBindings)).current;
  const [revision, setRevision] = useState(0);
  const configure = useCallback((value: BindingSettings) => {
    router.bindings.configure(value);
    setRevision(n => n + 1);
  }, [router]);
  const bindingsContext = useMemo(() => ({revision, configure}), [revision, configure]);
  const selection = useSelection();
  const manualInput=useRef(false),verificationTimers=useRef<ReturnType<typeof setTimeout>[]>([]);
  useLayoutEffect(() => { if (settings) router.bindings.configure(settings); }, [router, settings]);
  useLayoutEffect(() => () => router.bindings.configure({version: 1, overrides: {}}), [router]);
  // Explicit acceptance flags automate keys through the production router.
  // They never replace the renderer, scene, backend calls or approval policy.
  useEffect(() => {
    if(manualInput.current)return;
    const timers = (verificationInput || []).map(event => setTimeout(() => {
      if(!manualInput.current)router.dispatch({input: event.input, key: event.key});
    }, event.afterMs));
    verificationTimers.current=timers;
    return () => timers.forEach(clearTimeout);
  }, [router, verificationInput]);
  useLayoutEffect(() => router.register({ id: "clipboard.copy", priority: 10000, enabled: () => true,
    handle: ({ input, key }) => {
      if (!key.ctrl || input !== "c") return false;
      selection.copySelectionNoClear();
      return true; // Never let a clipboard shortcut reach task cancellation.
    } }), [router, selection]);
  useInput((input, key, event: any) => {
    // A person taking over a verification window owns it from this point on.
    // Never let a later test timer type a command into their new field/scene.
    manualInput.current=true;verificationTimers.current.forEach(clearTimeout);verificationTimers.current=[];
    router.dispatch({
      input,
      key: {...key, paste: Boolean(event?.keypress?.isPasted || (key as InputKey).paste)},
    });
  });
  return <RouterContext.Provider value={router}><BindingsContext.Provider value={bindingsContext}><ClipboardReaderContext.Provider value={clipboardReader}>{children}</ClipboardReaderContext.Provider></BindingsContext.Provider></RouterContext.Provider>;
}

export function InputLayer({ priority, bindingContext, children }: { priority: number; bindingContext?: string; children: React.ReactNode }) {
  const inherited = useContext(BindingScopeContext);
  return <PriorityContext.Provider value={priority}><BindingScopeContext.Provider value={bindingContext ?? inherited}>{children}</BindingScopeContext.Provider></PriorityContext.Provider>;
}

/** Retain mounted drafts and viewports while another view owns their input. */
export function InputSuspension({suspended,children}:{suspended:boolean;children:React.ReactNode}){
 const inherited=useContext(InputEnabledContext);
 return <InputEnabledContext.Provider value={inherited&&!suspended}>{children}</InputEnabledContext.Provider>;
}

/** Compatibility adapter: existing scene resolvers remain pure consumers.
 * Return false to defer to another registered owner; void means consumed.
 */
export function useManagedInput(handler: (input: string, key: InputKey) => void | boolean,
  options: { isActive?: boolean; priority?: number; clipboard?: boolean; inputTarget?:string; bindingContext?: string; raw?: boolean } = {}) {
  const router = useContext(RouterContext);
  const readOwnerClipboard = useContext(ClipboardReaderContext);
  useContext(BindingsContext); // Re-render hints together with the effective binding map.
  const layer = useContext(PriorityContext);
  const bindingScope = useContext(BindingScopeContext);
  const scopeEnabled=useContext(InputEnabledContext);
  options={...options,isActive:scopeEnabled&&options.isActive!==false};
  const id = useId();
  const current = useRef({ handler, options, bindingScope });
  const clipboardEpoch=useRef(0);
  useLayoutEffect(() => {
    const previous=current.current;
    if(previous.options.inputTarget!==options.inputTarget
      || (previous.options.isActive!==false)!==(options.isActive!==false)
      || (previous.options.clipboard!==false)!==(options.clipboard!==false)
      || (previous.options.bindingContext??previous.bindingScope)!==(options.bindingContext??bindingScope))clipboardEpoch.current++;
    current.current = { handler, options, bindingScope };
  });
  useLayoutEffect(() => {
    if (!router) throw new Error("INPUT_ROUTER_MISSING: mount InputProvider");
    let mounted = true;
    const unregister = router.register({ id, priority: layer + (options.priority || 0),
      enabled: () => current.current.options.isActive !== false,
      context: () => current.current.options.bindingContext ?? current.current.bindingScope,
      raw: () => Boolean(current.current.options.raw),
      handle: ({ input, key }) => {
        if (!current.current.options.raw && key.ctrl && input === "v" && current.current.options.clipboard !== false) {
          // OS IO may finish after Enter, Tab or a nested scene changed the
          // logical input target. A still-mounted component alone is not enough.
          const epoch=clipboardEpoch.current;
          void readOwnerClipboard().then(text => {
            if (mounted && epoch===clipboardEpoch.current && current.current.options.isActive !== false
              && current.current.options.clipboard!==false && text) current.current.handler(text, {paste: true});
          }).catch(() => undefined);
          return true;
        }
        return current.current.handler(input, key) !== false;
      } });
    return () => { mounted = false; unregister(); };
  }, [router, id, layer, options.priority,readOwnerClipboard]);
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
const ContentViewport = createContext<React.RefObject<ScrollBoxHandle | null> | null>(null);
export function useContentViewport() {return useContext(ContentViewport);}
function ScrollableContent({ children }: { children: React.ReactNode }) {
  const ref = useRef<ScrollBoxHandle>(null);
  useScrollInput(ref);
  return <ContentViewport.Provider value={ref}><ScrollBox ref={ref} flexDirection="column" flexGrow={1}>{children}</ScrollBox></ContentViewport.Provider>;
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
