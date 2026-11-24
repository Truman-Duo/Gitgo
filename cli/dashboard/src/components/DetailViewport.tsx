import React,{useLayoutEffect,useRef,useState} from "react";
import {Box,ScrollBox,measureElement,getElementRect,type DOMElement,type ScrollBoxHandle} from "@anthropic/ink";
import {ContentViewport,useContentViewport} from "../ui/ContentViewport.js";
import {useManagedInput} from "../input/runtime.js";
import {scrollViewport} from "../input/scrollViewport.js";
import {matchChord} from "../input/bindings.js";
import {colors,geometry} from "../theme/tokens.js";

type DetailContent=React.ReactNode|((width:number)=>React.ReactNode);

/** A read-only detail owns wheel input only inside its painted bounds. Its
 * unused delta goes to the original outer viewport, never a second listener. */
export function DetailViewport({children,onClose,enabled=true,framed=true,width,maxRows=geometry.record.detailMaxRows}: {
 children:DetailContent;onClose:()=>void;enabled?:boolean;framed?:boolean;width?:number;maxRows?:number;
}) {
 const parent=useContentViewport();
 // Static/non-scrolling consumers retain ordinary readable flow.
 return parent && enabled ? <BoundedDetail parent={parent} onClose={onClose} framed={framed} width={width} maxRows={maxRows}>{children}</BoundedDetail>
  : <>{typeof children==='function'?children(width||80):children}</>;
}
function BoundedDetail({parent,children,onClose,framed,width,maxRows}: {
 parent:React.RefObject<ScrollBoxHandle|null>;children:DetailContent;onClose:()=>void;framed:boolean;width?:number;maxRows:number;
}) {
 const ref=useRef<ScrollBoxHandle>(null),box=useRef<DOMElement>(null),content=useRef<DOMElement>(null),hover=useRef(false);
 const frame=framed?geometry.record.detailFrameInset*2:0;
 const limit=Math.max(1,Math.min(maxRows,(parent.current?.getViewportHeight()||maxRows+2+frame)-2-frame));
 const [rows,setRows]=useState(limit);
 const [measuredWidth,setMeasuredWidth]=useState<number>();
 const bodyWidth=Math.max(1,Math.min(width||Infinity,measuredWidth||width||80)-frame);
 useLayoutEffect(()=>{
  if(box.current){const next=Math.floor(measureElement(box.current).width);
   if(next>0)setMeasuredWidth(previous=>previous===next?previous:next);}
  if(content.current){const next=Math.max(1,Math.min(limit,Math.ceil(measureElement(content.current).height)));
   setRows(previous=>previous===next?previous:next);}
 });
 useManagedInput((input,key)=>{
  const wheel=key.wheelUp||key.wheelDown;
  const position=key.wheelPosition;
  const rect=box.current && getElementRect(box.current);
  const inside=position ? Boolean(rect && position.col>=rect.x && position.col<rect.x+rect.width
    && position.row>=rect.y && position.row<rect.y+rect.height) : hover.current;
  if(wheel ? !inside : !hover.current)return false;
  if(matchChord("escape",input,key)){onClose();return true;}
  const reveal=()=>{if(box.current)parent.current?.scrollIntoView(box.current);};
  if(key.home){reveal();ref.current?.scrollTo(0);return true;}
  if(key.end){reveal();ref.current?.scrollToBottom();return true;}
  const delta=(key.upArrow||key.pageUp||key.wheelUp?-1:key.downArrow||key.pageDown||key.wheelDown?1:0);
  if(!delta){hover.current=false;return false;}
  // A source may start below several unbounded headers. Reveal its complete
  // bounded window before scrolling inside; otherwise the last source line
  // can be logically reached yet physically clipped by the outer viewport.
  reveal();
  const remainder=scrollViewport(ref.current,delta*(key.pageUp||key.pageDown?Math.max(1,rows-1):wheel?3:1));
  if(remainder)scrollViewport(parent.current,remainder);
  return true;
 },{priority:100,clipboard:false});
 return <Box ref={box} flexDirection="column" flexShrink={0} minWidth={0}
  {...(framed?{borderStyle:'single' as const,borderColor:colors.divider.color}:{})}
  onMouseEnter={()=>{hover.current=true;}} onMouseLeave={()=>{hover.current=false;}}
  onClick={()=>{hover.current=true;}}>
  <ContentViewport scrollRef={ref}><ScrollBox ref={ref} height={Math.min(rows,limit)}
   flexDirection="column" flexShrink={0} flexGrow={0}>
   <Box ref={content} flexDirection="column" flexShrink={0} minWidth={0}>
    {typeof children==='function'?children(bodyWidth):children}
   </Box>
  </ScrollBox></ContentViewport>
 </Box>;
}
