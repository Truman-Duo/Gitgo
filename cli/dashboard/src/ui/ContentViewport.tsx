import React, {createContext, useContext} from "react";
import type {ScrollBoxHandle} from "@anthropic/ink";

const Viewport = createContext<React.RefObject<ScrollBoxHandle | null> | null>(null);
/** Shares the existing physical viewport. It creates no scroller or input owner. */
export function ContentViewport({scrollRef, children}: {
  scrollRef: React.RefObject<ScrollBoxHandle | null>; children: React.ReactNode;
}) {
  return <Viewport.Provider value={scrollRef}>{children}</Viewport.Provider>;
}
export function useContentViewport() {return useContext(Viewport);}
