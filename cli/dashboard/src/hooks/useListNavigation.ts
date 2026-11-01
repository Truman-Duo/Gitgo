import {useCallback, useLayoutEffect, useRef} from "react";
import type {ScrollBoxHandle} from "@anthropic/ink";
import {moveSelection, type Selectable} from "../ui/pageContract.js";
import {scrollViewport} from "../input/scrollViewport.js";

/** Selection remains bounded; unused arrow movement can browse read-only
 * content instead of trapping a page with zero/one available actions.
 * Keep the input cursor synchronous so a repeated key batch is not lost to
 * React batching. Viewport mutations never run inside a state updater.
 */
export function useListNavigation<Id extends string | number>(items: readonly Selectable<Id>[], selected: Id,
  onChange: (id: Id) => void, viewport: React.RefObject<ScrollBoxHandle | null>) {
  const cursor=useRef(selected);
  useLayoutEffect(()=>{cursor.current=selected;},[selected]);
  return useCallback((delta:number)=>{
    const next=moveSelection(items,cursor.current,delta);
    if(next !== undefined && next !== cursor.current){cursor.current=next;onChange(next);return;}
    scrollViewport(viewport.current,delta);
  },[items,onChange,viewport]);
}
