import type {LLMProvider} from "../../hooks/useLLMConfig.js";
export type Preferences={reasoning_effort?:string;body?:Record<string,unknown>};
export type Model={id:string;name:string;limits:{context_window?:number;max_output_capacity?:number};source?:string;field_sources?:Record<string,string>;metadata?:Record<string,unknown>};
export type Profile={model_id:string;protocol:string;context_window:number;max_output_tokens:number;request_preferences:Preferences;context_source:string;output_source:string;model_receipt?:string;probe_receipt?:string;deployment?:Model;verification?:unknown};
export type Connection=Record<string,string>;
export const protocols=["openai_chat","openai_responses","anthropic_messages","auto"];
export function loadedProfiles(seed?:LLMProvider):Profile[]{
 const raw=seed?.configuration?.models || (seed?.model_id?[{...seed,...seed.configuration}]:[]);
 return raw.map((p:any)=>({...p,request_preferences:p.request_preferences || {},context_source:p.sources?.context_window || p.context_source || "user",output_source:p.sources?.max_output_tokens || p.output_source || "user"}));
}
export function defaultProfile(model_id:string,protocol:string,deployment?:Model,receipt?:string):Profile{
 const context=deployment?.limits.context_window || 128000,capacity=deployment?.limits.max_output_capacity || 4096;
 return {model_id,protocol,context_window:context,max_output_tokens:Math.max(1,Math.min(4096,capacity,context-1)),request_preferences:{},context_source:deployment?.limits.context_window?(deployment.field_sources?.context_window==="https://models.dev/api.json"?"reference":"discovered"):"default",output_source:"default",deployment,model_receipt:receipt};
}
export function configurationJson(connection:Connection,profiles:Profile[]){
 return JSON.stringify({name:connection.name,preset_id:connection.preset,base_url:connection.base_url,protocol:connection.protocol,models:profiles.map(p=>({model_id:p.model_id,protocol:p.protocol,context_window:p.context_window,max_output_tokens:p.max_output_tokens,request_preferences:{reasoning_effort:p.request_preferences.reasoning_effort || "",body:p.request_preferences.body || {}}}))},null,2);
}
export function parseConfiguration(text:string,current:Connection,profiles:Profile[]):{connection:Connection;profiles:Profile[]}{
 const raw=JSON.parse(text);
 if(!raw||typeof raw!=="object"||Array.isArray(raw))throw Error("INVALID_CONFIGURATION_JSON");
 const allowed=["name","preset_id","base_url","protocol","models"];
 for(const key of Object.keys(raw))if(!allowed.includes(key))throw Error("UNKNOWN_CONFIGURATION_FIELD: "+key);
 for(const key of ["name","base_url","protocol"])if(typeof raw[key]!=="string"||!raw[key].trim())throw Error("INVALID_CONFIGURATION_FIELD: "+key);
 if(!protocols.includes(raw.protocol))throw Error("INVALID_PROVIDER_PROTOCOL: protocol");
 if(raw.preset_id!==undefined&&typeof raw.preset_id!=="string")throw Error("INVALID_CONFIGURATION_FIELD: preset_id");
 if(!Array.isArray(raw.models)||!raw.models.length||raw.models.length>10000)throw Error("INVALID_PROVIDER_MODELS: models");
 const seen=new Set<string>(),sameConnection=raw.base_url===current.base_url;
 const items=raw.models.map((row:any,index:number)=>{
  const path=`models[${index}]`;
  if(!row||typeof row!=="object"||Array.isArray(row))throw Error("INVALID_PROVIDER_MODEL: "+path);
  const permitted=["model_id","protocol","context_window","max_output_tokens","request_preferences"];
  for(const key of Object.keys(row))if(!permitted.includes(key))throw Error("UNKNOWN_MODEL_FIELD: "+path+"."+key);
  if(typeof row.model_id!=="string"||!row.model_id.trim()||row.model_id!==row.model_id.trim()||row.model_id.length>512||/[\x00-\x1f]/.test(row.model_id)||seen.has(row.model_id))throw Error("INVALID_OR_DUPLICATE_MODEL_ID: "+path+".model_id");
  seen.add(row.model_id);
  const old=profiles.find(p=>p.model_id===row.model_id),base=old || defaultProfile(row.model_id,raw.protocol);
  const protocol=row.protocol ?? raw.protocol,context=row.context_window ?? base.context_window,output=row.max_output_tokens ?? base.max_output_tokens;
  if(!protocols.includes(protocol))throw Error("INVALID_PROVIDER_PROTOCOL: "+path+".protocol");
  if(!Number.isInteger(context)||context<1024)throw Error("INVALID_CONTEXT_LIMIT: "+path+".context_window");
  if(!Number.isInteger(output)||output<1||output>=context)throw Error("INVALID_OUTPUT_BUDGET: "+path+".max_output_tokens");
  const prefs=row.request_preferences ?? {},body=prefs?.body ?? {},effort=prefs?.reasoning_effort ?? "";
  if(!prefs||typeof prefs!=="object"||Array.isArray(prefs)||Object.keys(prefs).some(k=>!["reasoning_effort","body"].includes(k))||typeof effort!=="string"||!body||typeof body!=="object"||Array.isArray(body))throw Error("INVALID_REQUEST_PREFERENCES: "+path+".request_preferences");
  const preferences={...(effort?{reasoning_effort:effort}:{}),...(Object.keys(body).length?{body}:{})};
  const unchanged=sameConnection&&base.protocol===protocol&&JSON.stringify(base.request_preferences)===JSON.stringify(preferences);
  return {...base,model_id:row.model_id,protocol,context_window:context,max_output_tokens:output,request_preferences:preferences,
   context_source:sameConnection&&context===base.context_window?base.context_source:"user",output_source:sameConnection&&output===base.max_output_tokens?base.output_source:"user",
   ...(!sameConnection||protocol!==base.protocol?{model_receipt:undefined,deployment:undefined}:{}),...(!unchanged?{probe_receipt:undefined,verification:undefined}:{})};
 });
 return {connection:{...current,name:raw.name,preset:raw.preset_id ?? current.preset,base_url:raw.base_url,protocol:raw.protocol},profiles:items};
}
export function manualModelIds(text:string):string[]{
 const ids=[...new Set(text.replace(/\r\n?/g,"\n").split("\n").map(id=>id.trim()).filter(Boolean))];
 if(!ids.length||ids.length>10000||ids.some(id=>id.length>512||/[\x00-\x1f]/.test(id)))throw Error("INVALID_PROVIDER_MODEL_ID");
 return ids;
}
