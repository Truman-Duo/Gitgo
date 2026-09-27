// src/hooks/usePoll.ts — Generic polling hook.
import { useEffect, useRef, useCallback } from "react";

export function usePoll(
  fn: () => Promise<void>,
  intervalMs: number,
  deps: unknown[],
) {
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const inFlightRef = useRef(false);
  const queuedRef = useRef(false);

  const stableFn = useCallback(fn, deps);

  useEffect(() => {
    let disposed = false;
    const tick = async () => {
      if (inFlightRef.current) {
        queuedRef.current = true;
        return;
      }
      inFlightRef.current = true;
      try {
        await stableFn();
      } finally {
        inFlightRef.current = false;
        if (queuedRef.current && !disposed) {
          queuedRef.current = false;
          void tick();
        }
      }
    };
    void tick();
    timerRef.current = setInterval(() => void tick(), intervalMs);
    return () => {
      disposed = true;
      queuedRef.current = false;
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [stableFn, intervalMs]);
}
