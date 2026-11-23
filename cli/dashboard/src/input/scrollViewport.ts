import type {ScrollBoxHandle} from "@anthropic/ink";

/** Retain coordinates already present in a parsed terminal wheel event. */
export function wheelPosition(sequence:string):{col:number;row:number}|undefined {
 const sgr=/^\x1b\[<(\d+);(\d+);(\d+)[Mm]$/.exec(sequence);
 if(sgr && (Number(sgr[1])&0x40))return {col:Number(sgr[2])-1,row:Number(sgr[3])-1};
 if(sequence.length===6 && sequence.startsWith("\x1b[M") && ((sequence.charCodeAt(3)-32)&0x40))
  return {col:sequence.charCodeAt(4)-33,row:sequence.charCodeAt(5)-33};
 return undefined;
}

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
