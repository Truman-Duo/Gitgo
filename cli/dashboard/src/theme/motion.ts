/** UI-only, cancellable interpolation; never delays task state or tool execution. */
export function animateNumber(
  from: number,
  to: number,
  onFrame: (value: number) => void,
  options: { durationMs: number; intervalMs: number },
) {
  if (options.durationMs <= 0 || from === to) {
    onFrame(to);
    return () => {};
  }
  const started = performance.now();
  const timer = setInterval(
    () => {
      const t = Math.min(1, (performance.now() - started) / options.durationMs);
      onFrame(from + (to - from) * (1 - (1 - t) ** 3));
      if (t === 1) clearInterval(timer);
    },
    Math.max(1, options.intervalMs),
  );
  return () => clearInterval(timer);
}
