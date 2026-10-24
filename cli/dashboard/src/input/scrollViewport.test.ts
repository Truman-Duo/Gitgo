import {test,expect} from "bun:test";
import type {ScrollBoxHandle} from "@anthropic/ink";
import {scrollViewport} from "./scrollViewport.js";
test("scroll remainder crosses a viewport edge without losing queued input",()=>{
 let top=3,pending=0;
 const view={getScrollHeight:()=>30,getViewportHeight:()=>10,getScrollTop:()=>top,
  getPendingDelta:()=>pending,scrollBy:(delta:number)=>{pending+=delta;}} as ScrollBoxHandle;
 expect(scrollViewport(view,-2)).toBe(0);expect(pending).toBe(-2);
 expect(scrollViewport(view,-3)).toBe(-2);expect(pending).toBe(-3);
 expect(scrollViewport(view,-1)).toBe(-1);expect(pending).toBe(-3);
 top=18;pending=0;
 expect(scrollViewport(view,5)).toBe(3);expect(pending).toBe(2);
 expect(scrollViewport(view,2)).toBe(2);expect(pending).toBe(2);
 expect(scrollViewport(null,-4)).toBe(-4);
});
