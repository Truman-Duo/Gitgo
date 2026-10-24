import type {ScrollBoxHandle} from "@anthropic/ink";

/** Consume a bounded scroll delta, including input waiting for Ink's next frame.
 * The returned remainder can continue into an adjacent viewport.
 */
export function scrollViewport(view:ScrollBoxHandle|null,delta:number):number {
 if(!view)return delta;
 const maximum=Math.max(0,view.getScrollHeight()-view.getViewportHeight());
 const top=Math.max(0,Math.min(maximum,view.getScrollTop()+view.getPendingDelta()));
 const next=Math.max(0,Math.min(maximum,top+delta));
 const consumed=next-top;
 if(consumed)view.scrollBy(consumed);
 return delta-consumed;
}
