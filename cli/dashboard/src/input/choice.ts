/** Form choices follow /config: show one value and cycle it with Left/Right. */
export function cycleChoiceIndex(index: number, delta: number, count: number): number {
  if (count <= 0) return 0;
  return ((Math.max(0, index) + delta) % count + count) % count;
}
