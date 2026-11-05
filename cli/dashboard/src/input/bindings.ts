/** Canonical event adapters for existing scene handlers. Physical keys live in the catalog. */
import {applicationBindings, BINDING_DEFINITIONS, INPUT_ACTIONS, matches, bindingLabel} from "./router.js";
export type Chord={label:string;match:(input:string,key:any)=>boolean};
const actions:Record<string,string>=Object.fromEntries(BINDING_DEFINITIONS.flatMap(d=>(d.chords||[]).map(chord=>[chord,d.id])));
export const CHORDS:Record<string,Chord>=Object.fromEntries(Object.entries(actions).map(([name,action])=>[name,{
 label:bindingLabel(INPUT_ACTIONS[action]),match:(input:string,key:any)=>matches({input,key},INPUT_ACTIONS[action]),
}]));
CHORDS.tabAny={label:"Tab",match:(input,key)=>CHORDS.tab.match(input,key)||CHORDS.shiftTab.match(input,key)};
CHORDS.upDown={label:"↑↓",match:(input,key)=>CHORDS.up.match(input,key)||CHORDS.down.match(input,key)};
CHORDS.leftRight={label:"←/→",match:(input,key)=>CHORDS.left.match(input,key)||CHORDS.right.match(input,key)};
// Legacy pure resolvers still use these canonical letters. Scoped live actions
// use isBindingAction and an explicit bindingContext instead of global letters.
for(const letter of ["l","h","r","d","x","s","v","a","p","n","e","t"]){
 CHORDS["letter"+letter.toUpperCase()]={label:letter.toUpperCase(),match:(input,key)=>!key?.paste&&input.toLowerCase()===letter};
}
CHORDS.shiftD={label:"Shift+D",match:(input,key)=>!key?.paste&&!!key.shift&&input.toLowerCase()==="d"};
export function matchChord(name:string,input:string,key:any):boolean {return CHORDS[name]?.match(input,key)||false;}
export function chordLabel(name:string):string {
 if(name==="upDown")return chordLabel("up")+chordLabel("down");
 if(name==="leftRight")return chordLabel("left")+"/"+chordLabel("right");
 return actions[name]?applicationBindings.label(actions[name]):CHORDS[name]?.label||name;
}
