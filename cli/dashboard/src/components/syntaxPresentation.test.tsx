import React from "react";
import {test, expect} from "bun:test";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {MarkdownText} from "./MarkdownText.js";
import {DiffView} from "./DiffView.js";
import {parseUnifiedDiff} from "../utils/diff.js";

// Inspect the product render buffer: the theme must survive nested ANSI and
// diff cell backgrounds in both layouts. This is not visible-terminal QA.
test("Dracula syntax is shared while only diff receives its background", async () => {
  const previous = chalk.level; chalk.level = 3;
  const stdin:any = new PassThrough(); Object.assign(stdin, {isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any = new PassThrough(); Object.assign(stdout,{isTTY:false,columns:100,rows:40});stdout.on("data",()=>{});
  const stderr:any = new PassThrough();stderr.on("data",()=>{});
  const source = 'def sample():\n    return "old"\n# sentinel\ncount = 42';
  const files = parseUnifiedDiff('--- a/sample.py\n+++ b/sample.py\n@@ -1,4 +1,4 @@\n def sample():\n-    return "old"\n+    return "new"\n # sentinel\n count = 42\n');
  const draw = (width:number,layout:"split"|"unified") => <Box width={96} height={40} flexDirection="column">
    <MarkdownText content={'```python\n'+source+'\n```'} width={96}/>
    <DiffView files={files} width={width} layout={layout}/>
  </Box>;
  const root = renderSync(draw(96,"unified"),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const ink:any = instances.get(stdout);
  function matches(needle:string){
    const screen = ink.frontFrame.screen, found:{x:number;y:number;fg:string;bg:string}[]=[];
    for(let y=0;y<screen.height;y++){
      const line = Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char??" ").join("");
      let x=-1;
      while((x=line.indexOf(needle,x+1))>=0){
        let fg="",bg="";
        for(const ansi of ink.stylePool.get(cellAt(screen,x,y)!.styleId)){
          for(const match of ansi.code.matchAll(/\x1b\[(38|48);2;(\d+);(\d+);(\d+)m/g)){
            if(match[1]==="38")fg=match.slice(2).join(",");else bg=match.slice(2).join(",");
          }
        }
        found.push({x,y,fg,bg});
      }
    }
    return found;
  }
  try{
    for(const [width,layout] of [[96,"unified"],[96,"split"],[40,"split"]] as const){
      root.rerender(draw(width,layout));await new Promise(resolve=>setTimeout(resolve,60));
      const keywords=matches("def sample");expect(keywords.length).toBeGreaterThanOrEqual(2);
      expect(keywords[0]!.bg).toBe("");
      for(const keyword of keywords){expect(keyword.fg).toBe("255,121,198");}
      for(const keyword of keywords.slice(1)){expect(keyword.bg).toBe("40,42,54");}
      for(const string of matches('"old"'))expect(string.fg).toBe("241,250,140");
      for(const comment of matches("# sentinel"))expect(comment.fg).toBe("98,114,164");
      for(const number of matches("42"))expect(number.fg).toBe("189,147,249");
      const removed=matches('"old"').slice(1),added=matches('"new"');
      expect(removed).toHaveLength(1);expect(added).toHaveLength(1);
      expect(removed[0]!.bg).toBe("45,26,26");expect(added[0]!.bg).toBe("18,38,30");
    }
  }finally{
    root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;
  }
});
