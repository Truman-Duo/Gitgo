import React,{useCallback,useEffect,useLayoutEffect,useRef,useState} from 'react';
import {Box,Text,type ScrollBoxHandle} from '@anthropic/ink';
import {colors} from '../theme/tokens.js';

export const SCROLLBAR_HIDE_DELAY_MS=3000;
export function scrollbarGeometry(top:number,total:number,rows:number){
 const height=Math.max(0,Math.floor(rows)),maximum=Math.max(0,total-height);
 const length=maximum?Math.max(1,Math.floor(height*height/Math.max(1,total))):height;
 const start=maximum?Math.round((height-length)*Math.max(0,Math.min(1,top/maximum))):0;
 return {height,maximum,length,start};
}
/** One gutter and one ScrollBox authority. Pointer capture owns a drag only;
 * visibility changes never resize or remount the optional reader children. */
export function ViewportScrollbar({scrollRef,children}:{
 scrollRef:React.RefObject<ScrollBoxHandle|null>;children?:React.ReactNode;
}){
 const [metrics,setMetrics]=useState({top:0,total:0,rows:0});
 const [visible,setVisible]=useState(false);
 const hover=useRef(false),drag=useRef<{offset:number}|null>(null);
 const timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
 const subscription=useRef<{handle:ScrollBoxHandle;stop:()=>void}|undefined>(undefined);
 const clearTimer=useCallback(()=>{if(timer.current)clearTimeout(timer.current);timer.current=undefined;},[]);
 const hideLater=useCallback(()=>{
  clearTimer();
  if(!hover.current&&!drag.current)timer.current=setTimeout(()=>{timer.current=undefined;setVisible(false);},SCROLLBAR_HIDE_DELAY_MS);
 },[clearTimer]);
 const reveal=useCallback(()=>{setVisible(true);hideLater();},[hideLater]);
 const enter=()=>{hover.current=true;clearTimer();setVisible(true);};
 const leave=()=>{hover.current=false;hideLater();};
 useEffect(()=>clearTimer,[clearTimer]);
 useLayoutEffect(()=>{
  const handle=scrollRef.current;
  if(subscription.current?.handle===handle)return;
  subscription.current?.stop();subscription.current=undefined;
  if(!handle)return;
  const update=()=>{
   const next={top:handle.getRenderedScrollTop(),total:handle.getScrollHeight(),rows:handle.getViewportHeight()};
   setMetrics(old=>old.top===next.top&&old.total===next.total&&old.rows===next.rows?old:next);
  };
  subscription.current={handle,stop:handle.subscribeMetrics(update)};update();
 });
 useLayoutEffect(()=>()=>{subscription.current?.stop();drag.current=null;},[]);
 const geometry=scrollbarGeometry(metrics.top,metrics.total,metrics.rows);
 const seek=(row:number,offset:number)=>{
  const handle=scrollRef.current;if(!handle)return;
  const current=scrollbarGeometry(handle.getRenderedScrollTop(),handle.getScrollHeight(),handle.getViewportHeight());
  if(!current.maximum)return;
  const ratio=Math.max(0,Math.min(1,(row-offset)/Math.max(1,current.height-current.length)));
  if(ratio===1)handle.scrollToBottom();else handle.scrollTo(Math.round(ratio*current.maximum));
 };
 const endDrag=()=>{drag.current=null;hideLater();};
 const track=<Box width={1} flexShrink={0} minHeight={0} flexDirection='column' overflow='hidden'
  onMouseEnter={enter} onMouseLeave={leave}
  onMouseDown={event=>{
   event.stopImmediatePropagation();reveal();
   if(!geometry.maximum)return;
   const inside=event.localRow>=geometry.start&&event.localRow<geometry.start+geometry.length;
   const offset=inside?event.localRow-geometry.start:Math.floor(geometry.length/2);
   drag.current={offset};clearTimer();
   if(!inside)seek(event.localRow,offset);
  }}
  onMouseDrag={event=>{event.stopImmediatePropagation();if(drag.current)seek(event.localRow,drag.current.offset);}}
  onMouseUp={event=>{event.stopImmediatePropagation();endDrag();}}>
  {Array.from({length:geometry.height},(_,row)=><Box key={row} height={1} flexShrink={0}>
   <Text color={row>=geometry.start&&row<geometry.start+geometry.length?colors.chat.reasoningFg:colors.chat.turnGuide}>
    {visible?(row>=geometry.start&&row<geometry.start+geometry.length?'┃':'│'):' '}
   </Text>
  </Box>)}
 </Box>;
 return <Box flexDirection={children?'row':'column'} width={children?undefined:1} flexGrow={children?1:0}
  flexShrink={children?1:0} minHeight={0} minWidth={0}>
  {children}{track}
 </Box>;
}
