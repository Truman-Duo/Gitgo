import type {TraceRecord} from "../traceView.js";
type Value = {events: TraceRecord[]; truncated: boolean; detail?: unknown};
const clients = new WeakMap<object, Map<string, {value: Value; at: number; estimatedBytes: number}>>();
const ttl = 30_000, maximumEntries = 24, maximumEvents = 100_000;
// Bound retained content as well as event count. This is a conservative
// content/object estimate, not a claim about the JS engine's total heap.
export const TRACE_CACHE_MAX_ESTIMATED_BYTES = 8 * 1024 * 1024;
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
  cache.delete(key);
  const serialized = JSON.stringify(value);
  const estimatedBytes = Math.max(Buffer.byteLength(serialized), serialized.length * 2) + value.events.length * 192;
  if (estimatedBytes > TRACE_CACHE_MAX_ESTIMATED_BYTES) return;
  cache.set(key, {value, at: Date.now(), estimatedBytes});
  let events = [...cache.values()].reduce((n, entry) => n + entry.value.events.length, 0);
  let bytes = [...cache.values()].reduce((n, entry) => n + entry.estimatedBytes, 0);
  while (cache.size > maximumEntries || events > maximumEvents || bytes > TRACE_CACHE_MAX_ESTIMATED_BYTES) {
    const oldest = cache.keys().next().value!;
    events -= cache.get(oldest)!.value.events.length;
    bytes -= cache.get(oldest)!.estimatedBytes; cache.delete(oldest);
  }
}
