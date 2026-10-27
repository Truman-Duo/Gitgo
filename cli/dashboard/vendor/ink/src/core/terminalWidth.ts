import {eastAsianWidth} from 'get-east-asian-width'

let widths = new Map<number, 1 | 2>()
export let terminalWidthRevision = 0
export let hasTerminalWidthOverrides = false

/** Install Host-observed widths before the first frame; never infer from locale. */
export function configureTerminalWidths(value: unknown): void {
  const next = new Map<number, 1 | 2>()
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value)
    if (entries.length <= 4096) for (const [key, width] of entries) {
      const point = Number(key)
      if (/^\d+$/.test(key) && Number.isInteger(point) && point >= 0xa0 && point < 0x10000
          && (width === 1 || width === 2)) next.set(point, width)
    }
  }
  widths = next
  hasTerminalWidthOverrides = next.size > 0
  terminalWidthRevision++
}

export function terminalCodepointWidth(point: number): number {
  return widths.get(point) ?? eastAsianWidth(point, {ambiguousAsWide: false})
}
