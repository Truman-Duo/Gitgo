import {useCallback, useEffect, useState,useSyncExternalStore} from "react";
import {useDisclosureScope} from './disclosureScope.js';
import type {SetStateAction} from "react";

export type ExpandedChange = SetStateAction<boolean>;
export const expandedValue = (current: boolean, change: ExpandedChange) =>
  typeof change === "function" ? change(current) : change;

/** Local presentation override; each queued toggle reads the last queued
 * state. A changed default retires old overrides without erasing a new click. */
export function useDisclosureState(defaultExpanded = false,identity?:string) {
  const [override, setOverride] = useState<{mode: boolean; expanded: boolean}>();
  const scope=useDisclosureScope(),store=identity?scope?.store:undefined,key=scope?.scope+'/'+identity;
  const subscribe=useCallback((notify:()=>void)=>store?store.subscribe(key,notify):()=>{},[store,key]);
  const snapshot=useCallback(()=>store?store.get(key):override,[store,key,override]);
  const choice=useSyncExternalStore(subscribe,snapshot,snapshot);
  const expanded = choice?.mode === defaultExpanded ? choice.expanded : defaultExpanded;
  useEffect(() => {
    if(store){if(store.get(key)?.mode!==defaultExpanded)store.set(key,undefined);return;}
    setOverride(current => current?.mode === defaultExpanded ? current : undefined);
  }, [defaultExpanded,store,key]);
  const setExpanded = useCallback((change: ExpandedChange) => {
    if(store){const current=store.get(key);store.set(key,{mode:defaultExpanded,
      expanded:expandedValue(current?.mode===defaultExpanded?current.expanded:defaultExpanded,change)});return;}
    setOverride(current => ({mode: defaultExpanded,
      expanded: expandedValue(current?.mode === defaultExpanded ? current.expanded : defaultExpanded, change)}));
  }, [defaultExpanded,store,key]);
  return [expanded, setExpanded] as const;
}
