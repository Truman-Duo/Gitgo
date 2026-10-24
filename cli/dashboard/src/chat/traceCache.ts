import type {TraceRecord} from "../traceView.js";
type Value = {events: TraceRecord[]; truncated: boolean};
const clients = new WeakMap<object, Map<string, {value: Value; at: number}>>();
const ttl = 30_000, maximumEntries = 24, maximumEvents = 100_000;
export function traceCacheKey(project: string, trace: string, process: string, descendants: boolean, status = "") {
  return JSON.stringify([project, trace, process, descendants, status]);
}
export function cachedTrace(client: object, key: string): Value | null {
  const cache = clients.get(client), entry = cache?.get(key);
  if (!entry || Date.now() - entry.at > ttl) {cache?.delete(key); return null;}
  cache!.delete(key); cache!.set(key, entry);
  return entry.value;
}
export function cacheTrace(client: object, key: string, value: Value) {
  let cache = clients.get(client);
  if (!cache) {cache = new Map(); clients.set(client, cache);}
  cache.delete(key); cache.set(key, {value, at: Date.now()});
  let events = [...cache.values()].reduce((n, entry) => n + entry.value.events.length, 0);
  while (cache.size > maximumEntries || events > maximumEvents) {
    const oldest = cache.keys().next().value!;
    events -= cache.get(oldest)!.value.events.length; cache.delete(oldest);
  }
}
