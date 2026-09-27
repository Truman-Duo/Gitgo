import {
  type RefObject,
  useCallback,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
} from 'react'
import type { ScrollBoxHandle } from '../components/ScrollBox.js'
import type { DOMElement } from '../core/dom.js'

export type VirtualWindow = {
  start: number
  end: number
  visibleStart: number
  visibleEnd: number
  before: number
  after: number
  total: number
}

export type VirtualScrollResult = VirtualWindow & {
  virtualized: boolean
  itemRef: (key: string) => (element: DOMElement | null) => void
}

type Options = {
  scrollRef: RefObject<ScrollBoxHandle | null>
  itemKeys: readonly string[]
  estimateSize?: number | ((key: string, index: number) => number)
  overscanRows?: number
  minimumItems?: number
  stickToEnd?: boolean
  layoutKey?: string | number
}

type CachedSize = { layoutKey: string | number; rows: number }
type LayoutAnchor = { key: string; offset: number; layoutKey: string | number }

function lowerBoundEnd(offsets: readonly number[], value: number): number {
  // First item whose end offset is strictly greater than value.
  let lo = 0
  let hi = Math.max(0, offsets.length - 1)
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((offsets[mid + 1] ?? 0) <= value) lo = mid + 1
    else hi = mid
  }
  return lo
}

export function calculateVirtualWindow(
  sizes: readonly number[],
  scrollTop: number,
  pendingDelta: number,
  viewportHeight: number,
  overscanRows: number,
): VirtualWindow {
  const count = sizes.length
  const offsets = new Array<number>(count + 1)
  offsets[0] = 0
  for (let index = 0; index < count; index += 1) {
    offsets[index + 1] = offsets[index]! + Math.max(1, Math.ceil(sizes[index] ?? 1))
  }
  const total = offsets[count] ?? 0
  if (count === 0) {
    return { start: 0, end: 0, visibleStart: 0, visibleEnd: 0, before: 0, after: 0, total: 0 }
  }

  // Cover both the currently painted viewport and the pending destination.
  // This is important because ScrollBox drains wheel input over several
  // frames; mounting only the destination produces a blank intermediate page.
  const current = Math.max(0, Math.min(scrollTop, Math.max(0, total - viewportHeight)))
  const target = Math.max(0, Math.min(
    scrollTop + pendingDelta,
    Math.max(0, total - viewportHeight),
  ))
  const visibleTop = Math.min(current, target)
  const visibleBottom = Math.min(total, Math.max(current, target) + Math.max(1, viewportHeight))
  const mountTop = Math.max(0, visibleTop - Math.max(0, overscanRows))
  const mountBottom = Math.min(total, visibleBottom + Math.max(0, overscanRows))
  const visibleStart = lowerBoundEnd(offsets, visibleTop)
  const visibleEnd = Math.min(count, lowerBoundEnd(offsets, Math.max(0, visibleBottom - 1)) + 1)
  const start = lowerBoundEnd(offsets, mountTop)
  const end = Math.min(count, lowerBoundEnd(offsets, Math.max(0, mountBottom - 1)) + 1)
  return {
    start,
    end: Math.max(start + 1, end),
    visibleStart,
    visibleEnd: Math.max(visibleStart + 1, visibleEnd),
    before: offsets[start] ?? 0,
    after: Math.max(0, total - (offsets[end] ?? total)),
    total,
  }
}

/**
 * Variable-height, row-based virtualization for ScrollBox.
 *
 * Item heights are measured from Yoga after each commit and cached by stable
 * item key plus layoutKey (normally the available width). Top/bottom spacers
 * preserve the full scroll coordinate space while only the visible window and
 * an overscan band remain mounted in React/Yoga.
 */
export function useVirtualScroll({
  scrollRef,
  itemKeys,
  estimateSize = 4,
  overscanRows = 16,
  minimumItems = 30,
  stickToEnd = false,
  layoutKey = 0,
}: Options): VirtualScrollResult {
  const [, invalidate] = useReducer((value: number) => value + 1, 0)
  const sizesRef = useRef(new Map<string, CachedSize>())
  const elementsRef = useRef(new Map<string, DOMElement>())
  const callbacksRef = useRef(new Map<string, (element: DOMElement | null) => void>())
  const lastLayoutKeyRef = useRef<string | number>(layoutKey)
  const pendingLayoutAnchorRef = useRef<LayoutAnchor | null>(null)

  const estimate = useCallback((key: string, index: number): number => {
    const raw = typeof estimateSize === 'function' ? estimateSize(key, index) : estimateSize
    return Math.max(1, Math.ceil(Number.isFinite(raw) ? raw : 1))
  }, [estimateSize])

  const readViewport = useCallback(() => {
    const handle = scrollRef.current
    const viewportHeight = Math.max(1, handle?.getViewportHeight() || 1)
    const top = Math.max(0, handle?.getScrollTop() || 0)
    const pending = handle?.getPendingDelta() || 0
    const sticky = handle?.isSticky() ?? stickToEnd
    return { viewportHeight, top, pending, sticky }
  }, [scrollRef, stickToEnd])

  useLayoutEffect(() => {
    const handle = scrollRef.current
    if (!handle) return
    const notify = () => invalidate()
    notify()
    return handle.subscribe(notify)
  }, [scrollRef])

  const viewport = readViewport()
  // Width changes invalidate every measured row count. Capture the semantic
  // item crossing the viewport before switching to the new estimates; a raw
  // scrollTop cannot survive text reflow and a DOM ref may be unmounted by the
  // virtual window during the same commit.
  if (lastLayoutKeyRef.current !== layoutKey) {
    if (!viewport.sticky && itemKeys.length > 0) {
      const previousLayoutKey = lastLayoutKeyRef.current
      const previousSizes = itemKeys.map((key, index) => {
        const cached = sizesRef.current.get(key)
        return cached?.layoutKey === previousLayoutKey ? cached.rows : estimate(key, index)
      })
      let before = 0
      let anchorIndex = 0
      for (; anchorIndex < previousSizes.length - 1; anchorIndex += 1) {
        const next = before + (previousSizes[anchorIndex] ?? 1)
        if (next > viewport.top) break
        before = next
      }
      const key = itemKeys[anchorIndex]
      if (key) pendingLayoutAnchorRef.current = {
        key, offset: Math.max(0, viewport.top - before), layoutKey,
      }
    }
    lastLayoutKeyRef.current = layoutKey
  }

  const sizes = itemKeys.map((key, index) => {
    const cached = sizesRef.current.get(key)
    return cached?.layoutKey === layoutKey ? cached.rows : estimate(key, index)
  })
  const estimatedTotal = sizes.reduce((sum, rows) => sum + rows, 0)
  const virtualized = itemKeys.length >= minimumItems
  // ScrollBox owns the live sticky state. `stickToEnd` is only the cold-start
  // default before its handle exists; manual scrolling clears stickiness and
  // an explicit scrollToBottom restores it even after streaming completed.
  const effectiveStickToEnd = viewport.sticky
  let effectiveTop = effectiveStickToEnd
    ? Math.max(0, estimatedTotal - viewport.viewportHeight)
    : viewport.top
  const pendingAnchor = pendingLayoutAnchorRef.current
  if (!effectiveStickToEnd && pendingAnchor?.layoutKey === layoutKey) {
    const anchorIndex = itemKeys.indexOf(pendingAnchor.key)
    if (anchorIndex >= 0) {
      effectiveTop = sizes.slice(0, anchorIndex).reduce((sum, rows) => sum + rows, 0)
        + pendingAnchor.offset
    }
  }
  const window = virtualized
    ? calculateVirtualWindow(
        sizes,
        effectiveTop,
        effectiveStickToEnd ? 0 : viewport.pending,
        viewport.viewportHeight,
        Math.max(overscanRows, viewport.viewportHeight),
      )
    : {
        start: 0,
        end: itemKeys.length,
        visibleStart: 0,
        visibleEnd: itemKeys.length,
        before: 0,
        after: 0,
        total: estimatedTotal,
      }

  const itemRef = useCallback((key: string) => {
    let callback = callbacksRef.current.get(key)
    if (!callback) {
      callback = (element: DOMElement | null) => {
        if (element) elementsRef.current.set(key, element)
        else elementsRef.current.delete(key)
      }
      callbacksRef.current.set(key, callback)
    }
    return callback
  }, [])

  useLayoutEffect(() => {
    let changed = false
    for (let index = window.start; index < window.end; index += 1) {
      const key = itemKeys[index]
      if (!key) continue
      const measured = Math.max(
        1,
        Math.ceil(elementsRef.current.get(key)?.yogaNode?.getComputedHeight() ?? 0),
      )
      const previous = sizesRef.current.get(key)
      if (!previous || previous.layoutKey !== layoutKey || previous.rows !== measured) {
        sizesRef.current.set(key, { layoutKey, rows: measured })
        changed = true
      }
    }

    const live = new Set(itemKeys)
    for (const key of sizesRef.current.keys()) {
      if (!live.has(key)) sizesRef.current.delete(key)
    }
    for (const key of callbacksRef.current.keys()) {
      if (!live.has(key)) callbacksRef.current.delete(key)
    }

    const handle = scrollRef.current
    if (virtualized && handle) {
      const viewportHeight = Math.max(1, handle.getViewportHeight() || viewport.viewportHeight)
      const mountedEnd = Math.max(window.before, window.total - window.after)
      handle.setClampBounds(window.before, Math.max(window.before, mountedEnd - viewportHeight))
    } else {
      handle?.setClampBounds(undefined, undefined)
    }
    const pendingAnchor = pendingLayoutAnchorRef.current
    if (handle && pendingAnchor?.layoutKey === layoutKey && !handle.isSticky()) {
      const anchorIndex = itemKeys.indexOf(pendingAnchor.key)
      if (anchorIndex >= 0) {
        const resolvedTop = itemKeys.slice(0, anchorIndex).reduce((sum, key, index) => {
          const cached = sizesRef.current.get(key)
          return sum + (cached?.layoutKey === layoutKey ? cached.rows : estimate(key, index))
        }, 0) + pendingAnchor.offset
        pendingLayoutAnchorRef.current = null
        handle.scrollTo(resolvedTop)
      }
    }
    if (changed) invalidate()
  })

  useLayoutEffect(() => () => {
    scrollRef.current?.setClampBounds(undefined, undefined)
  }, [scrollRef])

  return useMemo(() => ({ ...window, virtualized, itemRef }), [window.start, window.end,
    window.visibleStart, window.visibleEnd, window.before, window.after, window.total,
    virtualized, itemRef])
}
