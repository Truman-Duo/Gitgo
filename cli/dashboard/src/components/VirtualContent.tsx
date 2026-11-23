import React, {useRef} from "react";
import {Box, useVirtualScroll} from "@anthropic/ink";
import type {DOMElement, ScrollBoxHandle} from "@anthropic/ink";
import {useContentViewport} from "../ui/ContentViewport.js";

/** A measured content span in its parent's physical viewport. No new scroll
 * container, input listener, clamp owner, or business selection is created. */
export function VirtualContent({itemKeys, estimateRows = 4, layoutKey = 0, minimumItems = 30, children}: {
  itemKeys: readonly string[];
  estimateRows?: number | ((key: string, index: number) => number);
  layoutKey?: string | number; minimumItems?: number;
  children: (index: number) => React.ReactNode;
}) {
  const viewport = useContentViewport();
  const inactiveViewport = useRef<ScrollBoxHandle | null>(null);
  const container = useRef<DOMElement | null>(null);
  const virtual = useVirtualScroll({scrollRef: viewport || inactiveViewport, containerRef: container,
    manageClampBounds: false, itemKeys, estimateSize: estimateRows, layoutKey,
    minimumItems: viewport ? minimumItems : Infinity, overscanRows: 8});
  return <Box ref={container} flexDirection="column" flexShrink={0}>
    {virtual.before > 0 ? <Box height={virtual.before} flexShrink={0}/> : null}
    {itemKeys.slice(virtual.start, virtual.end).map((key, offset) => <Box key={key}
      ref={virtual.itemRef(key)} flexDirection="column" flexShrink={0}>
      {children(virtual.start + offset)}
    </Box>)}
    {virtual.after > 0 ? <Box height={virtual.after} flexShrink={0}/> : null}
  </Box>;
}
