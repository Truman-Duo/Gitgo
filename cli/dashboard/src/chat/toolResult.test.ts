import {expect,test} from "bun:test";
import {materializeToolDetail,toolOutputWithoutPatch,toolArgumentSummary} from "./toolResult.js";
import type {ToolCallCard} from "../types.js";
const patch="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n";
test("one call's exact diff is represented once across plain and structured formatted results",()=>{
  expect(toolOutputWithoutPatch("Saved.\n"+patch,patch)).toBe("Saved.");
  expect(JSON.parse(toolOutputWithoutPatch(JSON.stringify({path:"x.py",diff:patch}),patch))).toEqual({path:"x.py"});
  expect(toolOutputWithoutPatch(patch,patch)).toBe("");
  expect(toolOutputWithoutPatch("Another result without this patch",patch)).toBe("Another result without this patch");
});
test("file parameters are a readable inline path and requested range; body stays original source",()=>{
  const tool:ToolCallCard={tool_name:"read_file",target:"",allowed:true,status_label:"OK",duration_ms:1,timestamp:"",
    arguments:{path:"src/中文.ts",offset:10,limit:20}};
  expect(toolArgumentSummary(tool)).toBe("src/中文.ts:11–30");
  expect(materializeToolDetail(tool,{arguments:tool.arguments,formatted_result:'{"content":"wrapped"}',
    data:{content:"const original = '正文';"}}).source_text).toBe("const original = '正文';");
});
test("full recorded arguments/result/diff replace transport previews without changing the source objects",()=>{
  const tool:ToolCallCard={tool_name:"edit_file",target:"",allowed:true,status_label:"OK",duration_ms:1,timestamp:"",result_text:"preview"};
  const saved={arguments:{path:"original.py"},formatted_result:"full saved result",data:{diff:patch}};
  const original=JSON.stringify([tool,saved]);
  expect(materializeToolDetail(tool,{legacy_detail:saved})).toMatchObject({arguments:saved.arguments,result_text:saved.formatted_result,diff:patch});
  expect(JSON.stringify([tool,saved])).toBe(original);
});
