import {expect, spyOn, test} from "bun:test";
import {cachedTrace, cacheTrace, traceCacheKey, TRACE_CACHE_MAX_ESTIMATED_BYTES} from "./traceCache.js";

const value = (text: string) => ({truncated:false, events:[{seq:1, time:"now", event:"reasoning_delta", delta:text}]});

test("a single large record is rejected even though the cache contains only one event", () => {
  const client = {};
  cacheTrace(client, "huge", value("x".repeat(TRACE_CACHE_MAX_ESTIMATED_BYTES / 2)));
  expect(cachedTrace(client, "huge")).toBeNull();
});

test("content budget evicts the least recently used trace while protecting a recently opened one", () => {
  const client = {}, payload = "x".repeat(700_000);
  for (const key of ["a", "b", "c", "d", "e"]) cacheTrace(client, key, value(payload));
  expect(cachedTrace(client, "a")).not.toBeNull();
  cacheTrace(client, "f", value(payload));
  expect(cachedTrace(client, "a")).not.toBeNull();
  expect(cachedTrace(client, "b")).toBeNull();
  expect(cachedTrace(client, "f")).not.toBeNull();
});

test("keys keep project, process, descendants and completion state separate and expire without network writes", () => {
  const clock = spyOn(Date, "now").mockReturnValue(1_000);
  try {
    const client = {}, key = traceCacheKey("project", "trace", "a", false, "completed");
    cacheTrace(client, key, value("saved"));
    expect(cachedTrace({}, key)).toBeNull();
    expect(cachedTrace(client, traceCacheKey("other-project", "trace", "a", false, "completed"))).toBeNull();
    expect(cachedTrace(client, traceCacheKey("project", "trace", "a", true, "completed"))).toBeNull();
    expect(cachedTrace(client, key)).not.toBeNull();
    clock.mockReturnValue(31_001);
    expect(cachedTrace(client, key)).toBeNull();
  } finally {clock.mockRestore();}
});
