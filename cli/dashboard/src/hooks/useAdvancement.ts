import {useCallback, useEffect, useRef, useState} from "react";
import type {BackendClient} from "../backend/client.js";

export type Advancement = {mode: "auto" | "manual"; revision: number; phase: string;
  scope_id?: string; process_id?: string; error?: string; continuation_error?: string};

/** Backend-confirmed effective revisions only; late replies cannot change scenes. */
export function useAdvancement(client: BackendClient, project: string | null, processId = "",
  processes: Record<string, {advancement?: Advancement; parent_id?: string | null; actor_kind?: string; created_at?: string}> = {}) {
  const identity = `${project || ""}:${processId}`;
  const generation = useRef(0);
  const busy = useRef(false);
  const current = useRef<Advancement | null>(null);
  const [state, setState] = useState<{identity: string; value: Advancement | null; pending: boolean; error: string}>(
    {identity, value: null, pending: false, error: ""});
  const publish = useCallback((value: Advancement, token: number) => {
    if (token !== generation.current) return;
    if (!current.current || value.scope_id !== current.current.scope_id || value.revision >= current.current.revision) {
      current.current = value;
      setState(s => ({...s, identity, value}));
    }
  }, [identity]);
  useEffect(() => {
    const token = ++generation.current;
    current.current = null; busy.current = false;
    setState({identity, value: null, pending: false, error: ""});
    if (!project) return;
    let readBusy = false;
    const read = async () => {
      if (readBusy || busy.current) return;
      readBusy = true;
      try {
        const value = await client.callTool("runtime.advancement", {project, process_id: processId}, 35);
        publish(value, token);
        if (token === generation.current) setState(s => ({...s, error: ""}));
      } catch (error) {
        if (token === generation.current) setState(s => ({...s, error: String(error)}));
      } finally {readBusy = false;}
    };
    void read();
    return () => {if (generation.current === token) generation.current++;};
  }, [client, project, processId, identity, publish]);
  useEffect(() => {
    if (!project || busy.current) return;
    const id = current.current?.process_id;
    const observed = processes[processId || id || ""]?.advancement
      || (!processId && !id ? Object.values(processes)
        .filter(p => !p.parent_id && p.actor_kind === "supervisor")
        .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))[0]?.advancement : undefined);
    if (observed) publish({...observed, process_id: processId || id}, generation.current);
  }, [project, processId, processes, publish]);
  const toggle = useCallback(async () => {
    if (!project || busy.current || !current.current || current.current.phase !== "effective") return;
    const previous = current.current, token = generation.current;
    busy.current = true;
    setState(s => ({...s, pending: true, error: ""}));
    try {
      const value = await client.callTool("runtime.advancement", {project, process_id: processId,
        mode: previous.mode === "auto" ? "manual" : "auto", expected_revision: previous.revision}, 35);
      publish(value, token);
    } catch (error) {
      if (token === generation.current) setState(s => ({...s, error: String(error)}));
    } finally {
      if (token === generation.current) {busy.current = false; setState(s => ({...s, pending: false}));}
    }
  }, [client, project, processId, publish]);
  return {value: state.identity === identity ? state.value : null,
    pending: state.identity === identity && state.pending, error: state.identity === identity ? state.error : "", toggle};
}
