import {test} from "node:test";
import assert from "node:assert/strict";
import {applicationBindings} from "./router.js";
import {keyHintText} from "./hints.js";
import {copy} from "../ui/pageContract.js";

test("hint formatting is explicit, uses the effective map once, and leaves user text literal",()=>{
 const before=applicationBindings.snapshot();
 try{
  applicationBindings.configure({version:1,overrides:{"navigation.confirm":{key:"tab"},"navigation.tab":{key:"return"}}});
  assert.equal(keyHintText("Enter save · Tab next"),"Tab save · Enter next");
  assert.equal(copy({en:"Enter save · Tab next",zh:"Enter 保存 · Tab 下一项",keys:true},"en"),"Tab save · Enter next");
  assert.equal(copy({en:"User wrote Enter",zh:"用户写了 Enter"},"en"),"User wrote Enter");
  assert.equal(copy({en:"Enter saves, Esc cancels",zh:"Enter 保存，Esc 取消"},"en"),"Enter saves, Esc cancels");
 }finally{applicationBindings.configure(before);}
});
