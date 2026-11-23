import {expect,test} from "bun:test";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";
import {createHash} from "node:crypto";

const dir=resolve(import.meta.dir,"../../../assets/brand/generated"),pngHeader=Buffer.from([137,80,78,71,13,10,26,10]);
const png=(data:Buffer,size:number)=>{
 expect(data.subarray(0,8)).toEqual(pngHeader);expect(data.readUInt32BE(16)).toBe(size);expect(data.readUInt32BE(20)).toBe(size);
 expect(data[25]).toBe(6); // RGBA, preserving transparent edges and hand-drawn gaps.
};
test("every platform frame is bound to the accepted source and recorded bytes",()=>{
 const manifest=JSON.parse(readFileSync(resolve(dir,"manifest.json"),"utf8"));
 const source=readFileSync(resolve(dir,"../gitgo-icon.svg"));
 expect(createHash("sha256").update(source).digest("hex")).toBe(manifest.source.sha256);
 for(const file of manifest.files){const data=readFileSync(resolve(dir,file.name));expect(data.length).toBe(file.bytes);
  expect(createHash("sha256").update(data).digest("hex")).toBe(file.sha256);if(file.size)png(data,file.size);}
});
test("Windows ICO contains correctly indexed PNG frames at all small and high-DPI sizes",()=>{
 const data=readFileSync(resolve(dir,"gitgo.ico"));expect(data.readUInt16LE(2)).toBe(1);
 const count=data.readUInt16LE(4),sizes:number[]=[];
 for(let index=0;index<count;index++){const entry=6+index*16,size=data[entry]||256,offset=data.readUInt32LE(entry+12),bytes=data.readUInt32LE(entry+8);
  sizes.push(size);png(data.subarray(offset,offset+bytes),size);expect(offset+bytes).toBeLessThanOrEqual(data.length);}
 expect(sizes).toEqual([16,20,24,32,48,64,128,256]);
});
test("macOS ICNS includes standard and Retina PNG elements with bounded offsets",()=>{
 const data=readFileSync(resolve(dir,"gitgo.icns"));expect(data.subarray(0,4).toString()).toBe("icns");expect(data.readUInt32BE(4)).toBe(data.length);
 const expected:Record<string,number>={icp4:16,icp5:32,icp6:64,ic07:128,ic08:256,ic09:512,ic10:1024,ic11:32,ic12:64,ic13:256,ic14:512};
 let at=8;const found:string[]=[];
 while(at<data.length){const type=data.subarray(at,at+4).toString(),length=data.readUInt32BE(at+4);expect(length).toBeGreaterThan(8);
  expect(at+length).toBeLessThanOrEqual(data.length);png(data.subarray(at+8,at+length),expected[type]!);found.push(type);at+=length;}
 expect(at).toBe(data.length);expect(found.sort()).toEqual(Object.keys(expected).sort());
});
