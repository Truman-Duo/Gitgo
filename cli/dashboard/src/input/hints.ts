import {applicationBindings, bindingLabel, INPUT_ACTIONS, BINDING_DEFINITIONS} from "./router.js";

// Authored UI hints only. Never apply this function to drafts or model output.
const tokens:Record<string,string>=Object.fromEntries(BINDING_DEFINITIONS.flatMap(d=>(d.hintTokens||[]).map(token=>[token,d.id])));
const pattern=new RegExp(Object.keys(tokens).sort((a,b)=>b.length-a.length).map(k=>k.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|"),"g");
export function keyHintText(text:string):string {
  return text.replace(pattern,(token,offset)=>{
    if(/[A-Za-z]/.test(token[0]) && ((offset && /[A-Za-z]/.test(text[offset-1])) || /[A-Za-z]/.test(text[offset+token.length]||"")))return token;
    const id=tokens[token],effective=applicationBindings.label(id);
    return effective===bindingLabel(INPUT_ACTIONS[id])?token:effective;
  });
}
