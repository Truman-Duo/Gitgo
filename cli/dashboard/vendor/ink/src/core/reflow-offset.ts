/** Preserve the relative reading position inside a resized semantic block.
 * This preserves block identity, not an exact character/text selection offset.
 */
export function reflowOffset(offset: number, previousRows: number, nextRows: number): number {
  if (offset <= 0) return offset; // Preserve whitespace before the block too.
  const before = Math.max(1, previousRows);
  const after = Math.max(1, nextRows);
  return Math.min(Math.ceil(after) - 1, Math.floor(Math.min(offset, before - 1) * after / before));
}
