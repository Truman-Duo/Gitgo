import type { DOMElement } from './dom.js'
import {nodeCache} from './node-cache.js'

type Output = {
  /**
   * Element width.
   */
  width: number

  /**
   * Element height.
   */
  height: number
}

/**
 * Measure the dimensions of a particular `<Box>` element.
 */
const measureElement = (node: DOMElement): Output => ({
  width: node.yogaNode?.getComputedWidth() ?? 0,
  height: node.yogaNode?.getComputedHeight() ?? 0,
})

export default measureElement

/** Current painted bounds, clipped by physical ancestors. Input consumers
 * must not infer screen coordinates from unscrolled Yoga positions. */
export function getElementRect(node: DOMElement) {
  const cached = nodeCache.get(node);
  if (!cached) return undefined;
  let left=cached.x, top=cached.y, right=left+cached.width, bottom=top+cached.height;
  for (let parent=node.parentNode; parent; parent=parent.parentNode) {
    if (parent.style.overflow === 'hidden' || parent.style.overflow === 'scroll'
        || parent.style.overflowY === 'hidden' || parent.style.overflowY === 'scroll') {
      const rect=nodeCache.get(parent);
      if (!rect) return undefined;
      left=Math.max(left,rect.x); top=Math.max(top,rect.y);
      right=Math.min(right,rect.x+rect.width); bottom=Math.min(bottom,rect.y+rect.height);
    }
  }
  return right>left && bottom>top ? {x:left,y:top,width:right-left,height:bottom-top} : undefined;
}
