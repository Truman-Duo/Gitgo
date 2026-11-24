import {useLayoutEffect,useRef} from 'react';
import {getElementRect,type DOMElement} from '@anthropic/ink';
import {useContentViewport} from '../ui/ContentViewport.js';

/** Preserve the action's reading position after its new geometry commits. */
export function useDisclosureAction(expanded:boolean,onToggle:()=>void){
 const header=useRef<DOMElement>(null),viewport=useContentViewport();
 const pendingOffset=useRef<number|undefined>(undefined);
 useLayoutEffect(()=>{
  if(pendingOffset.current===undefined||!header.current)return;
  const offset=pendingOffset.current;pendingOffset.current=undefined;
  viewport?.current?.scrollToElement(header.current,-offset);
 });
 const toggle=()=>{
  const handle=viewport?.current,rect=header.current&&getElementRect(header.current);
  if(handle&&header.current&&rect){
   const row=Math.max(0,rect.y-handle.getViewportTop());
   pendingOffset.current=expanded?row:Math.min(row,Math.max(0,handle.getViewportHeight()-5));
   handle.scrollTo(handle.getScrollTop());
  }
  onToggle();
 };
 return {header,toggle};
}
