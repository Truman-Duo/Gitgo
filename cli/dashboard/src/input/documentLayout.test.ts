import {test,expect} from "bun:test";
import {documentLayout,documentCaret,moveDocumentLine} from "./documentLayout.js";

test("wraps without trimming indentation or changing source indices at spaces and CJK boundaries",()=>{
 const source='  "name": "甲乙👩‍💻 é long value"\n  "n": 42';
 const {rows,points}=documentLayout(source,10);
 for(const row of rows)expect(row.text).toBe(source.slice(row.start,row.end));
 expect(rows.map(row=>row.text).join("")).toBe(source.replaceAll("\n",""));
 for(const point of points){
  const row=rows[point.row]!;expect(point.index).toBeGreaterThanOrEqual(row.start);expect(point.index).toBeLessThanOrEqual(row.end);
  expect(documentCaret(points,point.index)).toEqual(point);
 }
 const wrap=rows[1]!.start;expect(documentCaret(points,wrap)).toMatchObject({row:1,column:0});
 expect(documentCaret(points,wrap-1).row).toBe(0);
});
test("visual up/down use the same wrap boundaries as rendering, including line ends and blank lines",()=>{
 const {points}=documentLayout('0123456789abcdefghij\n\n甲乙丙丁戊',10);
 expect(moveDocumentLine(points,16,-1)).toBe(6);
 expect(moveDocumentLine(points,6,1)).toBe(16);
 expect(documentCaret(points,20)).toMatchObject({row:1,column:10});
 expect(moveDocumentLine(points,20,1)).toBe(21);
 expect(moveDocumentLine(points,0,-100)).toBe(0);
 expect(moveDocumentLine(points,26,100)).toBe(26);
});
