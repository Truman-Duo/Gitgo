import {createHash, randomUUID} from "node:crypto";
import {mkdirSync, readFileSync, renameSync, writeFileSync, unlinkSync, statSync} from "node:fs";
import {dirname, join, resolve} from "node:path";
import {homedir} from "node:os";

// Presentation state is private to the local profile. It is never an execution
// request, Publish evidence, or part of the shared metadata projection.
export interface PublishLocalState {
  get<T>(key: string): T | undefined;
  set(key: string, value: unknown): void;
  remove(key: string): void;
  readonly error: string;
}
export interface PublishStateStore { scope(project: string, workspace?: string): PublishLocalState }
const MAX_BYTES = 1_048_576;
const hash = (value: string) => createHash("sha256").update(value).digest("hex");

export function createPublishStateStore(directory?: string): PublishStateStore {
  const scopes = new Map<string, PublishLocalState>();
  return {scope(project, workspace = "") {
    const identity = hash(JSON.stringify([project, workspace]));
    const previous = scopes.get(identity);
    if (previous) return previous;
    const file = directory ? join(directory, identity + ".json") : "";
    let entries: Record<string, unknown> = {}, error = "";
    if (file) try {
      if (statSync(file).size > MAX_BYTES) throw new Error("size");
      const data = JSON.parse(readFileSync(file, "utf8"));
      if (data.version !== 1 || data.identity !== identity || !data.entries || typeof data.entries !== "object" || Array.isArray(data.entries)) throw new Error("schema");
      entries = data.entries;
    } catch (failure: any) { if (failure.code !== "ENOENT") error = "PUBLISH_LOCAL_STATE_UNREADABLE"; }
    const save = (next: Record<string, unknown>) => {
      // Never serialize a token, including an accidentally supplied nested one.
      const raw = JSON.stringify({version: 1, identity, entries: next}, (key, value) => /token|password|secret/i.test(key) ? undefined : value);
      if (Buffer.byteLength(raw) > MAX_BYTES) { error = "PUBLISH_LOCAL_STATE_FULL"; return; }
      if (file) {
        const temporary = file + "." + randomUUID() + ".tmp";
        try {
          mkdirSync(dirname(file), {recursive: true, mode: 0o700});
          writeFileSync(temporary, raw, {encoding: "utf8", mode: 0o600, flag: "wx"});
          renameSync(temporary, file);
        } catch { try { unlinkSync(temporary); } catch {} error = "PUBLISH_LOCAL_STATE_UNSAVED"; return; }
      }
      entries = JSON.parse(raw).entries; error = "";
    };
    const state: PublishLocalState = {
      get<T>(key: string) { const value = entries[key]; return value === undefined ? undefined : structuredClone(value) as T; },
      set(key, value) { if (JSON.stringify(entries[key]) !== JSON.stringify(value)) save({...entries, [key]: value}); },
      remove(key) { if (!(key in entries)) return; const next = {...entries}; delete next[key]; save(next); },
      get error() { return error; },
    };
    scopes.set(identity, state); return state;
  }};
}

export function profilePublishStateStore(): PublishStateStore {
  const profile = resolve(process.env.GITGO_CONFIG_PATH || join(homedir(), ".gitgo", "config.json"));
  return createPublishStateStore(join(dirname(profile), "ui", "publish", hash(profile)));
}
