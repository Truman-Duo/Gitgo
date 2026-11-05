import type {HexColor} from "@anthropic/ink";

/** Shared RGB interpolation for color transitions and terminal tint backgrounds. */
export function lerpColor(a:HexColor,b:HexColor,t:number):HexColor {
 const parse=(hex:string)=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16));
 const first=parse(a),second=parse(b);
 return ("#"+first.map((value,i)=>Math.round(value+(second[i]!-value)*t).toString(16).padStart(2,"0")).join("")) as HexColor;
}
