import {stringWidth} from "@anthropic/ink";

export type DocumentRow={start:number;end:number;text:string;line:number;column:number;width:number};
export type DocumentPoint={index:number;row:number;column:number};
/** One lossless source-index map for rendering and caret motion. Never word-wrap
 * a prefix separately: its whitespace and break choices can differ from the body. */
export function documentLayout(value:string,rawWidth:number){
 const width=Math.max(1,Math.floor(rawWidth)),rows:DocumentRow[]=[],points:DocumentPoint[]=[{index:0,row:0,column:0}];
 const segments=Array.from(new Intl.Segmenter(undefined,{granularity:"grapheme"}).segment(value));
 let start=0,column=0,line=0,sourceColumn=0;
 for(const {segment,index} of segments){
  if(segment.includes("\n")){
   rows.push({start,end:index,text:value.slice(start,index),line,column:sourceColumn,width:column});
   line++;sourceColumn=0;column=0;start=index+segment.length;
  }else{
   const cells=stringWidth(segment);
   if(column>0&&column+cells>width){
    rows.push({start,end:index,text:value.slice(start,index),line,column:sourceColumn,width:column});
    sourceColumn+=column;start=index;column=0;
    points[points.length-1]={index,row:rows.length,column:0};
   }
   column+=cells;
  }
  points.push({index:index+segment.length,row:rows.length,column});
 }
 rows.push({start,end:value.length,text:value.slice(start),line,column:sourceColumn,width:column});
 return {rows,points};
}
export function documentCaret(points:DocumentPoint[],cursor:number):DocumentPoint{
 let left=0,right=points.length-1;
 while(left<right){const mid=Math.ceil((left+right)/2);if(points[mid]!.index<=cursor)left=mid;else right=mid-1;}
 return points[left]!;
}
export function moveDocumentLine(points:DocumentPoint[],cursor:number,delta:number){
 const current=documentCaret(points,cursor),target=Math.max(0,Math.min(points.at(-1)!.row,current.row+delta));
 if(target===current.row)return cursor;
 const candidates=points.filter(p=>p.row===target);
 return candidates.reduce((best,p)=>Math.abs(p.column-current.column)<Math.abs(best.column-current.column)?p:best,candidates[0]!).index;
}
