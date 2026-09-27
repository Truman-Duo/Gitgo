import {expect, test} from 'bun:test'
import {createScreen, StylePool, CharPool, HyperlinkPool, setCellAt, CellWidth} from './screen.js'
import {createSelectionState, startSelection, updateSelection, trackViewportScroll, getSelectedText} from './selection.js'

function screen(lines: string[]) {
  const result = createScreen(10, lines.length, new StylePool(), new CharPool(), new HyperlinkPool())
  lines.forEach((line, row) => [...line].forEach((char, col) => setCellAt(result, col, row,
    {char, width: CellWidth.Narrow, styleId: 0, hyperlink: undefined})))
  return result
}
const movement = (delta: number) => ({delta, viewportTop: 0, viewportBottom: 3, kind: 'manual' as const, layoutChanged: false})

test('manual scrolling grows a dragging selection beyond the viewport without duplicating rows', () => {
  const state = createSelectionState()
  startSelection(state, 0, 0); updateSelection(state, 9, 3)
  trackViewportScroll(state, screen(['one', 'two', 'three', 'four']), movement(2))
  expect(getSelectedText(state, screen(['three', 'four', 'five', 'six']))).toBe('one\ntwo\nthree\nfour\nfive\nsix')
  trackViewportScroll(state, screen(['three', 'four', 'five', 'six']), movement(1))
  expect(getSelectedText(state, screen(['four', 'five', 'six', 'seven']))).toBe('one\ntwo\nthree\nfour\nfive\nsix\nseven')
})

test('static footer selections are not shifted by conversation scrolling', () => {
  const state = createSelectionState()
  startSelection(state, 0, 5); updateSelection(state, 4, 5)
  trackViewportScroll(state, screen(['one', 'two', 'three', 'four', '', 'footer']), movement(2))
  expect(state.anchor?.row).toBe(5)
})

test('layout changes clear a stale screen-coordinate selection instead of copying replacement text', () => {
  const state = createSelectionState()
  startSelection(state, 0, 0); updateSelection(state, 4, 3)
  expect(trackViewportScroll(state, screen(['one', 'two', 'three', 'four']), {...movement(0), layoutChanged: true})).toBe(true)
  expect(state.anchor).toBeNull()
})
