// src/hooks/usePoll.ts — Generic polling hook.
import { useEffect, useRef, useCallback } from "react";

export function usePoll(
  fn: () => Promise<void>,
  intervalMs: number,
  deps: unknown[],
) {
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stableFn = useCallback(fn, deps);

  useEffect(() => {
    stableFn();
    timerRef.current = setInterval(stableFn, intervalMs);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [stableFn, intervalMs]);
}
