import {LogUpdate} from "../../cli/dashboard/vendor/ink/src/core/log-update.ts";
import {emptyFrame, type Frame} from "../../cli/dashboard/vendor/ink/src/core/frame.ts";
import {CharPool,HyperlinkPool,StylePool,createScreen,setCellAt,CellWidth} from "../../cli/dashboard/vendor/ink/src/core/screen.ts";
import {writeDiffToTerminal} from "../../cli/dashboard/vendor/ink/src/core/terminal.ts";
import {optimize} from "../../cli/dashboard/vendor/ink/src/core/optimizer.ts";
const styles=new StylePool(),chars=new CharPool(),links=new HyperlinkPool();
const frame=(lines:string[]):Frame=>{
 const screen=createScreen(20,lines.length,styles,chars,links);
 lines.forEach((line,y)=>Array.from(line).forEach((char,x)=>setCellAt(screen,x,y,{char,width:CellWidth.Narrow,styleId:styles.none,hyperlink:undefined})));
 return {screen,viewport:{width:20,height:8},cursor:{x:0,y:lines.length,visible:false}};
};
const target=frame(["A B","","new"]);
const serialize=(patches:any[])=>{
 let text="";
 writeDiffToTerminal({stdout:{write(value:string){text+=value}} as any,stderr:process.stderr},optimize(patches));
 return text;
};
const first=new LogUpdate({isTTY:true,stylePool:styles});
const initial=emptyFrame(8,20,styles,chars,links);
const start=frame(["A B"]);
const growing=new LogUpdate({isTTY:true,stylePool:styles});
const growthInitial=serialize(growing.render(initial,start));
console.log(JSON.stringify({first:serialize(first.render(initial,target)),growing:growthInitial+serialize(growing.render(start,target))}));
