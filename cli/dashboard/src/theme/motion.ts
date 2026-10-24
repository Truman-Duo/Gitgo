/** Shared UI-only frame source. Never schedules business or provider work. */
type Motion = {from: number; to: number; started: number; duration: number; interval: number;
  last: number; onFrame: (value: number) => void};
const motions = new Set<Motion>();
let timer: ReturnType<typeof setInterval> | undefined;
function releaseTimer() {
  if (!motions.size && timer !== undefined) {clearInterval(timer); timer = undefined;}
}
export function animateNumber(from: number, to: number, onFrame: (value: number) => void,
  options: {durationMs: number; intervalMs: number}) {
  if (options.durationMs <= 0 || from === to) {onFrame(to); return () => {};}
  const started = performance.now();
  const motion: Motion = {from, to, started, duration: options.durationMs,
    interval: Math.max(1, options.intervalMs), last: started, onFrame};
  motions.add(motion);
  if (timer === undefined) timer = setInterval(() => {
    const now = performance.now();
    for (const item of [...motions]) {
      const t = Math.min(1, (now - item.started) / item.duration);
      if (t < 1 && now - item.last < item.interval) continue;
      item.last = now;
      if (t === 1) motions.delete(item);
      item.onFrame(item.from + (item.to - item.from) * (1 - (1 - t) ** 3));
    }
    releaseTimer();
  }, 16);
  return () => {motions.delete(motion); releaseTimer();};
}
