/** Reproducible platform icons from the single accepted SVG design source. */
import {createHash} from "node:crypto";
import {readFileSync,mkdirSync,writeFileSync,renameSync,existsSync} from "node:fs";
import {resolve,relative} from "node:path";
import {createRequire} from "node:module";
import {initWasm,Resvg} from "@resvg/resvg-wasm";
import {assertRuntimeSecurity} from "./runtime.mjs";

assertRuntimeSecurity();
const require=createRequire(import.meta.url),root=resolve(import.meta.dir,"../../.."),brand=resolve(root,"assets/brand"),out=resolve(brand,"generated");
const SOURCE_SHA256="ed24abd2ea22f97c2c3964dd0103be41006b80e9d3deaf2fcf11a5ee93b72d9a";
const sizes=[16,20,24,32,48,64,128,256,512,1024];
const hash=(data:Uint8Array|string)=>createHash("sha256").update(data).digest("hex");
const source=readFileSync(resolve(brand,"gitgo-icon.svg")),svg=source.toString("utf8");
if(hash(source)!==SOURCE_SHA256)throw Error("BRAND_SOURCE_CHANGED: review the design identity before rebuilding icons");
if(/<(?:image|script|foreignObject)\b|(?:href\s*=|url\s*\()/i.test(svg))throw Error("BRAND_SOURCE_NOT_SELF_CONTAINED");
const version=JSON.parse(readFileSync(require.resolve("@resvg/resvg-wasm/package.json"),"utf8")).version;
if(version!=="2.6.2")throw Error("ICON_RENDERER_VERSION_CHANGED");
const wasm=readFileSync(require.resolve("@resvg/resvg-wasm/index_bg.wasm"));
const recipe={version:2,renderer:"@resvg/resvg-wasm",renderer_version:version,
 renderer_wasm_sha256:hash(wasm),converter_sha256:hash(readFileSync(import.meta.filename)),
 sizes,geometry:"source-viewBox",background:"transparent"};
const expectedNames=[...sizes.map(size=>`gitgo-${size}.png`),"gitgo.ico","gitgo.icns"].sort();
const manifestPath=resolve(out,"manifest.json");
function verify(){
 const manifest=JSON.parse(readFileSync(manifestPath,"utf8"));
 if(manifest.source.sha256!==SOURCE_SHA256||JSON.stringify(manifest.recipe)!==JSON.stringify(recipe))throw Error("ICON_RECIPE_CHANGED");
 if(!Array.isArray(manifest.files)||JSON.stringify(manifest.files.map((file:{name:string})=>file.name).sort())!==JSON.stringify(expectedNames))throw Error("ICON_INVENTORY_MISMATCH");
 for(const file of manifest.files){
  const target=resolve(out,file.name);
  const data=readFileSync(target);
  if(relative(out,target).startsWith("..")||data.length!==file.bytes||hash(data)!==file.sha256)throw Error("ICON_OUTPUT_MISMATCH: "+file.name);
 }
 return manifest;
}
if(process.argv.includes("--verify")){
 const manifest=verify();console.log(`Verified ${manifest.files.length} platform icon resources`);
}else{
 let reusable=false;
 if(existsSync(manifestPath)){try{verify();reusable=true;}catch{ /* Regenerate reproducible outputs from the locked source. */ }}
 if(reusable)console.log("Icon resources match the source, recipe and output hashes; reused");
 else{
  await initWasm(wasm);
  mkdirSync(out,{recursive:true});
  const pngs=new Map<number,Buffer>(),files:{name:string;sha256:string;bytes:number;size?:number}[]=[];
  const write=(name:string,data:Buffer,size?:number)=>{
   const target=resolve(out,name),temporary=target+".tmp";
   writeFileSync(temporary,data);renameSync(temporary,target);
   files.push({name,sha256:hash(data),bytes:data.length,...(size?{size}:{})});
  };
  for(const size of sizes){
   const renderer=new Resvg(svg,{font:{loadSystemFonts:false},fitTo:{mode:"width",value:size},shapeRendering:2});
   const image=renderer.render();
   try{
    if(image.width!==size||image.height!==size)throw Error("ICON_DIMENSION_MISMATCH");
    const data=Buffer.from(image.asPng());pngs.set(size,data);write(`gitgo-${size}.png`,data,size);
   }finally{image.free();renderer.free();}
  }
  const frames=sizes.filter(size=>size<=256),directory=Buffer.alloc(6+frames.length*16);
  directory.writeUInt16LE(1,2);directory.writeUInt16LE(frames.length,4);
  let offset=directory.length;
  frames.forEach((size,index)=>{
   const data=pngs.get(size)!,entry=6+index*16;
   directory[entry]=directory[entry+1]=size===256?0:size;
   directory.writeUInt16LE(1,entry+4);directory.writeUInt16LE(32,entry+6);
   directory.writeUInt32LE(data.length,entry+8);directory.writeUInt32LE(offset,entry+12);offset+=data.length;
  });
  write("gitgo.ico",Buffer.concat([directory,...frames.map(size=>pngs.get(size)!)]));
  const chunks:[string,number][]=[["icp4",16],["icp5",32],["icp6",64],["ic07",128],["ic08",256],["ic09",512],["ic10",1024],
   ["ic11",32],["ic12",64],["ic13",256],["ic14",512]];
  const records=chunks.map(([type,size])=>{const data=pngs.get(size)!,header=Buffer.alloc(8);header.write(type);header.writeUInt32BE(data.length+8,4);return Buffer.concat([header,data]);});
  const header=Buffer.alloc(8);header.write("icns");header.writeUInt32BE(8+records.reduce((bytes,record)=>bytes+record.length,0),4);
  write("gitgo.icns",Buffer.concat([header,...records]));
  const manifest={source:{name:"gitgo-icon.svg",sha256:SOURCE_SHA256},recipe,files};
  writeFileSync(manifestPath+".tmp",JSON.stringify(manifest,null,2)+"\n");renameSync(manifestPath+".tmp",manifestPath);
  console.log(`Generated ${files.length} platform icon resources`);
 }
}
const stageIndex=process.argv.indexOf("--stage");
if(stageIndex>=0){
 if(!process.argv[stageIndex+1])throw Error("ICON_STAGE_PATH_REQUIRED");
 const destination=resolve(process.argv[stageIndex+1]!);
 if(destination===brand||destination.startsWith(brand+"/")||destination.startsWith(brand+"\\"))throw Error("ICON_STAGE_OVERLAPS_SOURCE");
 const manifest=verify(),names=[...manifest.files.map((file:{name:string})=>file.name),"manifest.json"];
 // Preflight every target before writing; unknown existing files are never
 // overwritten. Packaging owns an empty stage, not a user's installation.
 for(const name of names){const target=resolve(destination,name);
  if(existsSync(target)&&hash(readFileSync(target))!==hash(readFileSync(resolve(out,name))))throw Error("ICON_STAGE_CONFLICT: "+name);}
 mkdirSync(destination,{recursive:true});
 for(const name of names){const target=resolve(destination,name);
  if(!existsSync(target))writeFileSync(target,readFileSync(resolve(out,name)),{flag:"wx"});}
 console.log("Staged verified platform icon resources");
}
