import React,{createContext,useContext,useMemo,useRef} from 'react';
export type DisclosureChoice={mode:boolean;expanded:boolean};
export function createDisclosureStore(){
 const values=new Map<string,DisclosureChoice>(),listeners=new Map<string,Set<()=>void>>();
 return {values,get:(key:string)=>values.get(key),
  set(key:string,value:DisclosureChoice|undefined){const old=values.get(key);if(old?.mode===value?.mode&&old?.expanded===value?.expanded)return;
   if(value)values.set(key,value);else values.delete(key);for(const notify of listeners.get(key)||[])notify();},
  subscribe(key:string,notify:()=>void){let group=listeners.get(key);if(!group){group=new Set();listeners.set(key,group);}group.add(notify);
   return()=>{group!.delete(notify);if(!group!.size)listeners.delete(key);};},
  clearPrefix(prefix:string){for(const key of values.keys())if(key.startsWith(prefix)){
   values.delete(key);for(const notify of listeners.get(key)||[])notify();}},
  clearMatching(matches:(key:string)=>boolean){for(const key of values.keys())if(matches(key)){
   values.delete(key);for(const notify of listeners.get(key)||[])notify();}},
  prune(scopes:readonly string[]){const live=new Set(scopes);for(const key of values.keys())if(!live.has(key.split('/')[0]!))values.delete(key);}
 };
}
export type DisclosureStore=ReturnType<typeof createDisclosureStore>;
const Context=createContext<{store:DisclosureStore;scope:string}|undefined>(undefined);
/** UI state lives above virtual rows; scope identities never include geometry. */
export function DisclosureScope({scope,store,children}:{scope:string;store?:DisclosureStore;children:React.ReactNode}){
 const parent=useContext(Context),own=useRef<DisclosureStore|undefined>(undefined);
 if(!own.current)own.current=createDisclosureStore();
 const selected=store||parent?.store||own.current;
 const prefix=store?scope:parent?parent.scope+'/'+scope:scope;
 const value=useMemo(()=>({store:selected,scope:prefix}),[selected,prefix]);
 return <Context.Provider value={value}>{children}</Context.Provider>;
}
export const useDisclosureScope=()=>useContext(Context);
