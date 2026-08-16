// src/hooks/useAsyncPoll.ts
// Shared async lifecycle: loading + error state with a try/catch/finally runner.
// Polling (usePoll) remains separate; this hook owns only the load/error plumbing.

import { useState, useCallback } from "react";

export function useAsyncPoll(initialLoading = false) {
  const [loading, setLoading] = useState(initialLoading);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async <T>(fn: () => Promise<T>, opts?: { loading?: boolean }): Promise<T | null> => {
      if (opts?.loading ?? true) setLoading(true);
      try {
        const result = await fn();
        setError(null);
        return result;
      } catch (e: any) {
        setError(e.message);
        return null;
      } finally {
        setLoading(false);
      }
    },
    []
  );

  return { loading, error, run, setError };
}
