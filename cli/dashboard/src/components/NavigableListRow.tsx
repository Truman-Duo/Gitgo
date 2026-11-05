import React,{useLayoutEffect,useRef,type ComponentProps} from "react";
import {useTerminalSize,type DOMElement} from "@anthropic/ink";
import {useContentViewport} from "../input/runtime.js";
import {SelectionRow} from "./Selection.js";

/** Selection presentation and reveal behavior; input remains owned by the scene. */
export function NavigableListRow(props:ComponentProps<typeof SelectionRow>){
 const viewport=useContentViewport(),row=useRef<DOMElement>(null),size=useTerminalSize();
 useLayoutEffect(()=>{if(props.selected&&row.current)viewport?.current?.scrollIntoView(row.current);},[props.selected,size.columns,size.rows,viewport]);
 return <SelectionRow {...props} ref={row}/>;
}
