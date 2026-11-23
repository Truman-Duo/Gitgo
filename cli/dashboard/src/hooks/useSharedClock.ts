import {useCallback, useMemo, useSyncExternalStore} from "react";

type Clock = {now: number; listeners: Set<() => void>; timer?: ReturnType<typeof setInterval>};
const clocks = new Map<number, Clock>();
/** Presentation time only. Runtime deadlines and business polling stay separate. */
export function useSharedClock(intervalMs = 100, enabled = true) {
  const period = Number.isFinite(intervalMs) ? Math.max(16, Math.round(intervalMs)) : 100;
  const clock = useMemo(() => {
    let clock = clocks.get(period);
    if (!clock) {clock = {now: Date.now(), listeners: new Set()}; clocks.set(period, clock);}
    return clock;
  }, [period]);
  const subscribe = useCallback((listener: () => void) => {
    if (!enabled) return () => {};
    clock.listeners.add(listener);
    if (clock.timer === undefined) {
      clock.now = Date.now();
      clock.timer = setInterval(() => {
        clock.now = Date.now();
        for (const notify of clock.listeners) notify();
      }, period);
    }
    return () => {
      clock.listeners.delete(listener);
      if (!clock.listeners.size && clock.timer !== undefined) {
        clearInterval(clock.timer); clock.timer = undefined;
      }
    };
  }, [clock, period, enabled]);
  const snapshot = useCallback(() => enabled ? clock.now : 0, [clock, enabled]);
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
