import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, Text, TerminalRaster, renderSync, type Raster} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {parseMultipleKeypresses, INITIAL_STATE} from "../../vendor/ink/src/core/parse-keypress.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
const settle=()=>new Promise(resolve=>setTimeout(resolve,90));
test("cell-size replies are responses, including fragmented reads, never editable input",()=>{
  const [first,state]=parseMultipleKeypresses(INITIAL_STATE,"\x1b[6;16;");
  expect(first).toEqual([]);
  const [next]=parseMultipleKeypresses(state,"8tx");
  expect(next[0]).toMatchObject({kind:"response",response:{type:"cellPixelSize",width:8,height:16}});
  expect(next[1]?.kind).toBe("key");
});
test("normal Ink frames own raster paint, stable frames, resize clipping and unmount clearing",async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode:()=>stdin,ref:()=>stdin,unref:()=>stdin});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:80,rows:24});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  let output="";stdout.on("data",(chunk:Buffer)=>output+=chunk.toString());
  const image:Raster={width:16,height:16,palette:["#000000","#ffffff"],pixels:new Uint8Array(256).fill(1)};
  const view=(visible=true)=><Box flexDirection="column" height={24}>
    <Text>Header</Text>{visible?<TerminalRaster image={image} columns={40} rows={4}/>:<Text>Returned content</Text>}<Text>Footer</Text>
  </Box>;
  const root=renderSync(view(),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const ink:any=instances.get(stdout);
  try{
    ink.setAltScreenActive(true,false);root.rerender(view());await settle();
    expect(ink.frontFrame.rasters).toHaveLength(1);expect(output).toContain("\x1bP0;1;0q");
    output="";root.rerender(view());await settle();expect(output).not.toContain("\x1bP0;1;0q");
    output="";stdout.columns=20;stdout.emit("resize");root.rerender(view());await settle();
    expect(ink.frontFrame.rasters).toHaveLength(0);expect(output).not.toContain("\x1bP0;1;0q");
    stdout.columns=80;stdout.emit("resize");root.rerender(view());await settle();
    expect(ink.frontFrame.rasters).toHaveLength(1);
    output="";root.rerender(view(false));await settle();
    expect(ink.frontFrame.rasters).toHaveLength(0);
    expect(output).toContain("\x1b[2J");
    expect(Array.from({length:80},(_,x)=>cellAt(ink.frontFrame.screen,x,1)?.char??"").join("")).toContain("Returned content");
    expect(output).not.toContain("\x1bP0;1;0q");
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
