import { expect, test } from 'bun:test'
import { calculateVirtualWindow } from './use-virtual-scroll.js'

test('virtual window mounts only the visible rows plus overscan', () => {
  const range = calculateVirtualWindow(new Array(100).fill(4), 160, 0, 20, 8)
  expect(range.start).toBe(38)
  expect(range.end).toBe(47)
  expect(range.before).toBe(152)
  expect(range.after).toBe(212)
  expect(range.visibleStart).toBe(40)
  expect(range.visibleEnd).toBe(45)
})

test('virtual window covers pending scroll destination and current viewport', () => {
  const range = calculateVirtualWindow(new Array(100).fill(2), 20, 20, 10, 2)
  expect(range.start).toBeLessThanOrEqual(9)
  expect(range.end).toBeGreaterThanOrEqual(25)
  expect(range.before + range.after).toBeLessThan(range.total)
})

test('virtual window handles an empty list', () => {
  expect(calculateVirtualWindow([], 0, 0, 10, 5)).toEqual({
    start: 0, end: 0, visibleStart: 0, visibleEnd: 0,
    before: 0, after: 0, total: 0,
  })
})
