import wrapAnsiNpm from 'wrap-ansi'
import stripAnsi from 'strip-ansi'
import {stringWidth} from './stringWidth.js'
import {hasTerminalWidthOverrides} from './terminalWidth.js'
import {getGraphemeSegmenter} from './utils/grapheme.js'
import sliceAnsi from './utils/sliceAnsi.js'

type WrapAnsiOptions = {
  hard?: boolean
  wordWrap?: boolean
  trim?: boolean
}

const wrapAnsi: (
  input: string,
  columns: number,
  options?: WrapAnsiOptions,
) => string = (input, columns, options) => {
  const wrapped = wrapAnsiNpm(input, columns, options)
  if (!hasTerminalWidthOverrides || !options?.hard || columns < 1) return wrapped
  // wrap-ansi uses its own Unicode widths. Keep its word wrapping and ANSI
  // style/link handling, then split any rows that exceed observed cell widths.
  return wrapped.split('\n').flatMap(line => {
    if (stringWidth(line) <= columns) return [line]
    const rows: string[] = []
    let start = 0, position = 0, used = 0
    for (const {segment} of getGraphemeSegmenter().segment(stripAnsi(line))) {
      const width = stringWidth(segment)
      if (used > 0 && used + width > columns) {
        rows.push(sliceAnsi(line, start, position))
        start = position
        used = 0
      }
      // A two-cell glyph cannot fit a one-cell viewport. Keep its position
      // without emitting half a glyph into a neighboring layout region.
      if (width > columns) {
        rows.push(' '.repeat(columns))
        position += width
        start = position
        continue
      }
      position += width
      used += width
    }
    if (used > 0) rows.push(sliceAnsi(line, start, position))
    return rows
  }).join('\n')
}

export { wrapAnsi }
