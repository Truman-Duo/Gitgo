import {useEffect,useState} from "react";
import {useTextInput} from "./useTextInput.js";
import {useModuleHelp} from "../components/ModuleHelpBoundary.js";
import {applyCommandAction,resolveCommandKeys} from "../input/commandInput.js";
import {useCopy} from "../ui/PageFrame.js";
import type {FooterConfig,Suggestion} from "../components/CommandBar.js";
import type {HelpTopic} from "../ui/moduleHelp.js";
import {matchChord} from "../input/bindings.js";
/** Scoped help and optional existing module commands, using the standard router.
 * Only mount this adapter where no other command owner is active, never in text editing. */
export function useLocalHelpCommand(topic:HelpTopic,onFooter:((value:FooterConfig|null)=>void)|undefined=undefined,active=true,operations?:{suggest:(input:string)=>Suggestion[];run:(command:string)=>void}){
 const help=useModuleHelp(),input=useTextInput(""),t=useCopy(),[suggestionIdx,setSuggestionIdx]=useState(0);
 const footer=onFooter??help?.onFooter;
 const suggestions=operations?operations.suggest(input.value):input.value&&"/help".startsWith(input.value)?[{label:t({en:"/help",zh:"/help"}),description:t({en:"Help",zh:"帮助"}),inputMode:"execute" as const}]:[];
 const index=Math.min(suggestionIdx,Math.max(0,suggestions.length-1));
 useEffect(()=>setSuggestionIdx(0),[input.value]);
 useEffect(()=>{if(!active||!input.value||!footer)return;footer({kind:"command",preserveStatus:true,cmdInput:input,suggestions,suggestionIdx:index,cmdResult:"",statusText:""});return()=>footer(null);},[active,input,footer,t,index]);
 const handle=(text:string,key:Record<string,any>)=>{
  if(!active||!help)return false;
  if(key.paste){if(!input.value)return false;input.insertText(text,{paste:true});return true;}
  if(!input.value&&!text.startsWith("/"))return false;
  if(matchChord("escape",text,key)){input.setValue("");return true;}
  for(const action of resolveCommandKeys(input.value,suggestions.length,text,key,suggestions[index]?.label||"",suggestions[index]?.inputMode||"fill"))applyCommandAction(action,{cmdInput:input,suggestionLabels:suggestions.map(s=>s.label),suggestionIdx:index,setSuggestionIdx,runCommand:command=>{input.setValue("");if(command==="/help")help.open(topic);else operations?.run(command);}});
  return true;
 };
 return {handle,active:Boolean(input.value)};
}
