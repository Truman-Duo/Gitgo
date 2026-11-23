import type {ToolCallCard} from "../types.js";

export function materializeToolDetail(tool: ToolCallCard, saved: unknown): ToolCallCard {
  if (!saved || typeof saved !== "object") return tool;
  const outer=saved as Record<string,unknown>;
  const detail=(outer.legacy_detail && typeof outer.legacy_detail === "object" ? outer.legacy_detail : outer) as Record<string,unknown>;
  const data=detail.data && typeof detail.data === "object" ? detail.data as Record<string,unknown> : {};
  return {...tool,
    ...(detail.arguments !== undefined ? {arguments:detail.arguments} : {}),
    ...(typeof detail.formatted_result === "string" ? {result_text:detail.formatted_result} : {}),
    ...(typeof data.content === "string" ? {source_text:data.content} : {}),
    ...(typeof data.diff === "string" ? {diff:data.diff} : {}),
  };
}

/** Short source-derived parameters beside the tool name. Long payloads stay
 * in the original record; never serialize them merely to paint a header. */
export function toolArgumentSummary(tool:ToolCallCard,maximum=120):string {
  const original=tool.arguments ?? tool.argument_preview;
  const args=original && typeof original==="object" && !Array.isArray(original)
    ? original as Record<string,unknown> : null;
  const brief=(value:unknown)=>typeof value==="string"
    ? value.slice(0,maximum*3).replace(/[\x00-\x1f\x7f-\x9f]/g," ").replace(/\s+/g," ").trim()
    : typeof value==="number"||typeof value==="boolean" ? String(value) : "";
  if(args && typeof args.path==="string"){
    let path=brief(args.path);
    if(tool.tool_name==="read_file" && (Number.isSafeInteger(args.offset)||Number.isSafeInteger(args.limit))){
      const start=typeof args.offset==="number"?Math.max(0,args.offset)+1:1;
      path+=":"+start+(typeof args.limit==="number"?"–"+(start+Math.max(1,args.limit)-1):"…");
    }
    return path;
  }
  if(args){
    const primary=["url","query","command","question","operation"].find(key=>typeof args[key]==="string");
    if(primary)return brief(args[primary]);
    const short=Object.entries(args).slice(0,3).map(([key,value])=>{
      const text=brief(value);return text?key+"="+text:Array.isArray(value)?key+"["+value.length+"]":key+"={…}";
    }).join(" · ");
    if(short)return short;
  }
  return brief(tool.target);
}

/** Remove only an identical patch representation from this call's output.
 * Independent calls and non-identical content are never merged by text. */
export function toolOutputWithoutPatch(result = "", patch = ""): string {
  if (!patch) return result;
  const text=result.replace(/\r\n/g,"\n"), exact=patch.replace(/\r\n/g,"\n");
  if (text.includes(exact)) return text.replace(exact,"").trim();
  try {
    const data=JSON.parse(result);
    if (data && typeof data === "object" && !Array.isArray(data) && data.diff?.replace?.(/\r\n/g,"\n") === exact) {
      const {diff,...remaining}=data;
      return Object.keys(remaining).length ? JSON.stringify(remaining,null,2) : "";
    }
  } catch { /* Plain or incomplete original output remains readable. */ }
  return result;
}
