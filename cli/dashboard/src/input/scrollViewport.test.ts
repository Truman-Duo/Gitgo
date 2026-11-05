import {test} from "node:test";
import assert from "node:assert/strict";
import type {ScrollBoxHandle} from "@anthropic/ink";
import {scrollViewport} from "./scrollViewport.js";
test("scroll remainder crosses a viewport edge without losing queued input",()=>{
 let top=3,pending=0;
 const view={getScrollHeight:()=>30,getViewportHeight:()=>10,getScrollTop:()=>top,
  getPendingDelta:()=>pending,scrollBy:(delta:number)=>{pending+=delta;}} as ScrollBoxHandle;
 assert.equal(scrollViewport(view,-2),0);assert.equal(pending,-2);
 assert.equal(scrollViewport(view,-3),-2);assert.equal(pending,-3);
 assert.equal(scrollViewport(view,-1),-1);assert.equal(pending,-3);
 top=18;pending=0;
 assert.equal(scrollViewport(view,5),3);assert.equal(pending,2);
 assert.equal(scrollViewport(view,2),2);assert.equal(pending,2);
 assert.equal(scrollViewport(null,-4),-4);
});
