import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
} from 'react'
import type { ScrollBoxHandle } from '../components/ScrollBox.js'
import type { DOMElement } from '../core/dom.js'
import { reflowOffset } from '../core/reflow-offset.js'

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
  /** A virtual span inside the same ScrollBox, not another scroll owner. */
  containerRef?: RefObject<DOMElement | null>
  manageClampBounds?: boolean
}

type CachedSize = { layoutKey: string | number; rows: number }
type LayoutAnchor = { key: string; offset: number; rows: number; layoutKey: string | number }
// Descendant layout effects precede their ancestors. Within one commit the
// deepest restored span owns the precise position; an ancestor must not replace
// it with a message-sized estimate. This state never survives the commit batch.
const restoredSpans = new WeakMap<ScrollBoxHandle, {top: number}>()
function restoreSpan(handle: ScrollBoxHandle, top: number, nested: boolean) {
  handle.scrollTo(top)
  if (!nested) return
  const entry = {top}
  restoredSpans.set(handle, entry)
  queueMicrotask(() => {if (restoredSpans.get(handle) === entry) restoredSpans.delete(handle)})
}

export function virtualContentOffset(element: DOMElement | null): number {
  let offset = 0
  for (let node = element; node; node = node.parentNode ?? null) {
    if (node.style.overflowY === 'scroll' || node.style.overflow === 'scroll') break
    offset += node.yogaNode?.getComputedTop() ?? 0
  }
  return Math.max(0, Math.round(offset))
}

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
  containerRef,
  manageClampBounds = !containerRef,
}: Options): VirtualScrollResult {
  const [, invalidate] = useReducer((value: number) => value + 1, 0)
  const sizesRef = useRef(new Map<string, CachedSize>())
  const elementsRef = useRef(new Map<string, DOMElement>())
  const callbacksRef = useRef(new Map<string, (element: DOMElement | null) => void>())
  const lastLayoutKeyRef = useRef<string | number>(layoutKey)
  const pendingLayoutAnchorRef = useRef<LayoutAnchor | null>(null)
  const previousSizesRef = useRef(new Map<string, number>())
  const subscriptionRef = useRef<{handle: ScrollBoxHandle; unsubscribe: () => void} | null>(null)

  const estimate = useCallback((key: string, index: number): number => {
    const raw = typeof estimateSize === 'function' ? estimateSize(key, index) : estimateSize
    return Math.max(1, Math.ceil(Number.isFinite(raw) ? raw : 1))
  }, [estimateSize])

  const readViewport = useCallback(() => {
    const handle = scrollRef.current
    const viewportHeight = Math.max(1, handle?.getViewportHeight() || 1)
    const offset = containerRef ? virtualContentOffset(containerRef.current) : 0
    const globalTop = Math.max(0, containerRef && handle?.isSticky()
      ? handle.getFreshScrollHeight() - viewportHeight : handle?.getScrollTop() || 0)
    const top = Math.max(0, globalTop - offset)
    const pending = handle?.getPendingDelta() || 0
    const sticky = handle?.isSticky() ?? stickToEnd
    return { viewportHeight, top, pending, sticky, offset, globalTop }
  }, [scrollRef, stickToEnd, containerRef])

  // A span may run before ScrollBox installs its imperative handle. Check
  // after every commit, but subscribe only when the actual viewport changes.
  // An unchanged ref object can point at a replacement ScrollBox after async
  // empty -> history hydration; listening to the old handle freezes the window.
  useEffect(() => {
    const handle = scrollRef.current
    if (subscriptionRef.current?.handle === handle) return
    subscriptionRef.current?.unsubscribe()
    subscriptionRef.current = null
    if (handle) {
      const stopScroll=handle.subscribe(() => invalidate())
      let previousHeight=handle.getScrollHeight(),previousViewport=handle.getViewportHeight()
      // Child-local disclosures and lazy bodies can change Yoga geometry
      // without rendering their virtual parent. Reconcile cached row heights
      // and clamp coverage after that paint, rather than waiting for another
      // wheel/key event (which otherwise remains stuck at an old clamp).
      const stopMetrics=handle.subscribeMetrics(()=>{
        const height=handle.getScrollHeight(),viewport=handle.getViewportHeight()
        if(height===previousHeight&&viewport===previousViewport)return
        previousHeight=height;previousViewport=viewport;invalidate()
      })
      subscriptionRef.current = {handle, unsubscribe:()=>{stopScroll();stopMetrics();}}
      invalidate()
    }
  })
  useEffect(() => () => {
    subscriptionRef.current?.unsubscribe()
    subscriptionRef.current = null
  }, [])

  const viewport = readViewport()
  // Width changes invalidate every measured row count. Capture the semantic
  // item crossing the viewport before switching to the new estimates; a raw
  // scrollTop cannot survive text reflow and a DOM ref may be unmounted by the
  // virtual window during the same commit.
  if (lastLayoutKeyRef.current !== layoutKey) {
    if (!viewport.sticky && itemKeys.length > 0 && (!containerRef || viewport.globalTop >= viewport.offset)) {
      const previousLayoutKey = lastLayoutKeyRef.current
      const previousSizes = itemKeys.map((key, index) => {
        const cached = sizesRef.current.get(key)
        return cached?.layoutKey === previousLayoutKey ? cached.rows
          : previousSizesRef.current.get(key) ?? estimate(key, index)
      })
      let before = 0
      let anchorIndex = 0
      for (; anchorIndex < previousSizes.length - 1; anchorIndex += 1) {
        const next = before + (previousSizes[anchorIndex] ?? 1)
        if (next > viewport.top) break
        before = next
      }
      const key = itemKeys[anchorIndex]
      if (key && (!containerRef || viewport.top < previousSizes.reduce((sum, value) => sum + value, 0))) pendingLayoutAnchorRef.current = {
        key, offset: Math.max(0, viewport.top - before), rows: previousSizes[anchorIndex] ?? 1, layoutKey,
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
  const effectiveStickToEnd = !containerRef && viewport.sticky
  let effectiveTop = effectiveStickToEnd
    ? Math.max(0, estimatedTotal - viewport.viewportHeight)
    : viewport.top
  const pendingAnchor = pendingLayoutAnchorRef.current
  if (!effectiveStickToEnd && pendingAnchor?.layoutKey === layoutKey) {
    const anchorIndex = itemKeys.indexOf(pendingAnchor.key)
    if (anchorIndex >= 0) {
      effectiveTop = sizes.slice(0, anchorIndex).reduce((sum, rows) => sum + rows, 0)
        + reflowOffset(pendingAnchor.offset, pendingAnchor.rows, sizes[anchorIndex] ?? 1)
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
    // A lazy body above the reading position may grow without a width change.
    // Retain the first visible key and its line offset, rather than its old
    // numeric position in the span.
    let measuredAnchor: {index: number; offset: number} | undefined
    if (containerRef && !viewport.sticky && viewport.pending === 0
        && !pendingLayoutAnchorRef.current && viewport.globalTop >= viewport.offset
        && viewport.top < estimatedTotal) {
      let before = 0
      for (let index = 0; index < sizes.length; index += 1) {
        if (before + sizes[index]! > viewport.top) {
          measuredAnchor = {index, offset: viewport.top - before}
          break
        }
        before += sizes[index]!
      }
    }
    for (let index = window.start; index < window.end; index += 1) {
      const key = itemKeys[index]
      if (!key) continue
      const height = elementsRef.current.get(key)?.yogaNode?.getComputedHeight() ?? 0
      // A retained conversation underneath a full-screen overlay has zero
      // Yoga height. It was not remeasured as one line: preserve its cache.
      if (!Number.isFinite(height) || height <= 0) continue
      const measured = Math.max(1, Math.ceil(height))
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

    // Retain estimates too: most of a virtualized history has never mounted.
    // Using the next width's estimates to locate the old viewport shifts the
    // semantic anchor even when all measured rows are cached correctly.
    previousSizesRef.current = new Map(itemKeys.map((key, index) => {
      const cached = sizesRef.current.get(key)
      return [key, cached?.layoutKey === layoutKey ? cached.rows : sizes[index]!]
    }))

    const handle = scrollRef.current
    if (manageClampBounds && virtualized && handle) {
      const viewportHeight = Math.max(1, handle.getViewportHeight() || viewport.viewportHeight)
      const mountedEnd = Math.max(window.before, window.total - window.after)
      handle.setClampBounds(window.before, Math.max(window.before, mountedEnd - viewportHeight))
    } else if (manageClampBounds) {
      handle?.setClampBounds(undefined, undefined)
    }
    const pendingAnchor = pendingLayoutAnchorRef.current
    if (handle && pendingAnchor?.layoutKey === layoutKey && !handle.isSticky()) {
      const anchorIndex = itemKeys.indexOf(pendingAnchor.key)
      if (anchorIndex >= 0) {
        const resolvedTop = itemKeys.slice(0, anchorIndex).reduce((sum, key, index) => {
          const cached = sizesRef.current.get(key)
          return sum + (cached?.layoutKey === layoutKey ? cached.rows : estimate(key, index))
        }, 0) + reflowOffset(pendingAnchor.offset, pendingAnchor.rows,
          sizesRef.current.get(pendingAnchor.key)?.rows ?? sizes[anchorIndex] ?? 1)
        pendingLayoutAnchorRef.current = null
        // A deeper span may already have restored a more precise anchor in
        // this commit. Do not replace it with an ancestor's coarse estimate.
        const restored = restoredSpans.get(handle)
        if (!restored || Math.abs(handle.getScrollTop() - restored.top) >= 1) {
          restoreSpan(handle, viewport.offset + resolvedTop, Boolean(containerRef))
        }
      }
    }
    if (changed && handle && measuredAnchor && !handle.isSticky()
        && Math.abs(handle.getScrollTop() - viewport.globalTop) < 1) {
      const before = itemKeys.slice(0, measuredAnchor.index).reduce((sum, key, index) => {
        const cached = sizesRef.current.get(key)
        return sum + (cached?.layoutKey === layoutKey ? cached.rows : sizes[index]!)
      }, 0)
      const rows = sizesRef.current.get(itemKeys[measuredAnchor.index]!)?.rows ?? sizes[measuredAnchor.index]!
      const resolved = before + Math.min(measuredAnchor.offset, rows - 1)
      if (resolved !== viewport.top) restoreSpan(handle, viewport.offset + resolved, true)
    }
    if (changed || containerRef && virtualContentOffset(containerRef.current) !== viewport.offset) invalidate()
  })

  useLayoutEffect(() => () => {
    if (manageClampBounds) scrollRef.current?.setClampBounds(undefined, undefined)
  }, [scrollRef, manageClampBounds])

  return useMemo(() => ({ ...window, virtualized, itemRef }), [window.start, window.end,
    window.visibleStart, window.visibleEnd, window.before, window.after, window.total,
    virtualized, itemRef])
}
