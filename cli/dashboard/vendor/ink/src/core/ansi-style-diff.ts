import {diffAnsiCodes as upstreamDiff, type AnsiCode} from '@alcalzone/ansi-tokenize'

/** SGR 22 clears BOTH intensity bits. Restore any target bit that an undo
 * removed even when that bit was already present in the source style.
 */
export function diffAnsiCodes(from: AnsiCode[], to: AnsiCode[]): AnsiCode[] {
  const diff = upstreamDiff(from, to)
  if (!diff.some(code => code.code === '\x1b[22m')) return diff
  const emitted = new Set(diff.map(code => code.code))
  return [...diff, ...to.filter(code => code.endCode === '\x1b[22m' && !emitted.has(code.code))]
}
