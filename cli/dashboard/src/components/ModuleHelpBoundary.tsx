import React,{createContext,useCallback,useContext,useRef,useState} from "react";
import {Box,useTerminalSize} from "@anthropic/ink";
import {InputLayer,InputSuspension} from "../input/runtime.js";
import {HelpPanel} from "./HelpPanel.js";
import type {HelpTopic} from "../ui/moduleHelp.js";
import type {FooterConfig} from "./CommandBar.js";
import {footerDrawerHeight} from "../ui/overlayLayout.js";
type HelpControl={open:(topic:HelpTopic)=>void;onFooter:(value:FooterConfig|null)=>void};
const ModuleHelpContext=createContext<HelpControl|null>(null);
export const useModuleHelp=()=>useContext(ModuleHelpContext);
/** Retains the original component tree and footer while a scoped help view owns input. */
export function ModuleHelpBoundary({children,onFooter,inline=false}:{children:(onFooter:(value:FooterConfig|null)=>void)=>React.ReactNode;onFooter:(value:FooterConfig|null)=>void;inline?:boolean}){
 const [topic,setTopic]=useState<HelpTopic|null>(null),openRef=useRef(false),latest=useRef<FooterConfig|null>(null),{rows}=useTerminalSize();
 const receive=useCallback((value:FooterConfig|null)=>{latest.current=value;if(!openRef.current)onFooter(value);},[onFooter]);
 const open=useCallback((value:HelpTopic)=>{openRef.current=true;setTopic(value);onFooter(inline?(latest.current&&!latest.current.hidden?{...latest.current,readOnly:true}:null):{hidden:true});},[onFooter,inline]);
 const close=useCallback(()=>{openRef.current=false;setTopic(null);onFooter(latest.current);},[onFooter]);
 return <ModuleHelpContext.Provider value={{open,onFooter:receive}}>
  <Box display={topic?"none":"flex"} flexDirection="column" flexGrow={inline?0:1} minHeight={0}><InputSuspension suspended={Boolean(topic)}>{children(receive)}</InputSuspension></Box>
  {topic?<Box flexDirection="column" flexGrow={1} minHeight={0} height={inline?footerDrawerHeight(rows):undefined}><InputLayer priority={1000}><HelpPanel scene="projects" topic={topic} onDismiss={close}/></InputLayer></Box>:null}
 </ModuleHelpContext.Provider>;
}
