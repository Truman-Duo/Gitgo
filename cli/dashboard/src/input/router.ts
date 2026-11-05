import catalog from "../../../../backend/resources/keyboard/actions.json";
/** Terminal input control plane. One event, one owner; views never read stdin. */
export type InputKey = Record<string, any>;
export type InputEvent = { input: string; key: InputKey };
export type InputScope = {
  id: string;
  priority: number;
  enabled: () => boolean;
  context?: () => string;
  raw?: () => boolean;
  handle: (event: InputEvent) => boolean;
};

export type BindingSpec = { key: string; ctrl?: boolean; shift?: boolean; meta?: boolean; super?: boolean };
export type BindingSettings = { version: 1; overrides: Record<string, BindingSpec> };
export type KeyboardPreset = "standard" | "portable";
export function presetSettings(preset: KeyboardPreset): BindingSettings {
  return {version: 1, overrides: preset === "portable" ? {
    "editor.newline": {key: "return", meta: true},
    "navigation.previousTab": {key: "p", ctrl: true},
  } : {}};
}

/** Catalog-backed action bindings consumed by the dispatcher and config editor.
 * The canonical events keep existing scene-specific command resolvers compatible.
 */
export type BindingDefinition = {id: string; default: BindingSpec; aliases?: BindingSpec[]; contexts: string[];
  chords?: string[]; hintTokens?: string[]; label: {en:string;zh:string}; condition: {en:string;zh:string}};
export const BINDING_DEFINITIONS: BindingDefinition[] = catalog;
export const INPUT_ACTIONS: Record<string, BindingSpec> = Object.fromEntries(catalog.map(d => [d.id,d.default]));
const definitions = Object.fromEntries(BINDING_DEFINITIONS.map(d=>[d.id,d]));
const namedKeys = ["escape","return","tab","upArrow","downArrow","leftArrow","rightArrow","pageUp","pageDown","backspace","delete","home","end"];
export function bindingLabel(binding: BindingSpec): string {
  const names: Record<string,string> = {escape:"Esc",return:"Enter",tab:"Tab",upArrow:"↑",downArrow:"↓",leftArrow:"←",rightArrow:"→",pageUp:"PgUp",pageDown:"PgDn",backspace:"Backspace",delete:"Del",home:"Home",end:"End"," ":"Space"};
  return [binding.ctrl?"Ctrl":"",binding.shift?"Shift":"",binding.meta?"Alt":"",binding.super?"Super":"",names[binding.key]||binding.key.toUpperCase()].filter(Boolean).join("+");
}
/** Decode only a key event actually delivered by Ink. Silence is not evidence of support. */
export function bindingFromEvent(event: InputEvent): BindingSpec | null {
  if(event.key.paste || event.key.wheelUp || event.key.wheelDown || event.key.mouse) return null;
  const key = namedKeys.find(name=>event.key[name]) || (event.input.length===1 && event.input.charCodeAt(0)>=32 ? event.input.toLowerCase() : "");
  if(!key)return null;
  return {key, ...(event.key.ctrl?{ctrl:true}:{}), ...(event.key.shift?{shift:true}:{}),
    ...(event.key.meta && !event.key.escape?{meta:true}:{}), ...(event.key.super?{super:true}:{})};
}
function applicable(action: string, context: string) {
  return definitions[action].contexts.some(value=>value==="*" || value===context);
}

export function signature(binding: BindingSpec) {
  return `${binding.key.toLowerCase()}:${+!!binding.ctrl}:${+!!binding.shift}:${+!!binding.meta}:${+!!binding.super}`;
}
export function matches(event: InputEvent, binding: BindingSpec) {
  if(event.key.paste)return false;
  return (binding.key.length === 1
    ? event.input.toLowerCase() === binding.key.toLowerCase()
    : Boolean(event.key[binding.key]))
    && !!event.key.ctrl === !!binding.ctrl && !!event.key.shift === !!binding.shift
    && !!(event.key.meta && !event.key.escape) === !!binding.meta
    && !!event.key.super === !!binding.super;
}

function variants(action:string,binding:BindingSpec):BindingSpec[] {
  return signature(binding)===signature(INPUT_ACTIONS[action]) ? [binding,...(definitions[action].aliases||[])] : [binding];
}

export class InputBindingRegistry {
  private bindings = { ...INPUT_ACTIONS };
  configure(settings: BindingSettings) {
    if (!settings || Object.keys(settings).some(k=>!["version","overrides"].includes(k)) || settings.version !== 1 || !settings.overrides || typeof settings.overrides !== "object" || Array.isArray(settings.overrides)) throw new Error("INPUT_BINDING_VERSION_UNSUPPORTED");
    const next = { ...INPUT_ACTIONS };
    for (const [action, binding] of Object.entries(settings.overrides)) {
      if (!Object.hasOwn(INPUT_ACTIONS, action) || !binding || typeof binding.key !== "string" ||
          Object.keys(binding).some(k=>!["key","ctrl","shift","meta","super"].includes(k)) ||
          [binding.ctrl,binding.shift,binding.meta,binding.super].some(v=>v!==undefined && typeof v!=="boolean") ||
          !(namedKeys.includes(binding.key) || (binding.key.length===1 && binding.key.charCodeAt(0)>=32 && binding.key.charCodeAt(0)<=126)))
        throw new Error(`INPUT_BINDING_INVALID: ${action}`);
      if(definitions[action].contexts.includes("*") && binding.key.length===1 && !binding.ctrl && !binding.meta && !binding.super)
        throw new Error(`INPUT_BINDING_TEXT_CONFLICT: ${action}`);
      next[action] = {...binding,key:binding.key.length===1?binding.key.toLowerCase():binding.key};
    }
    const entries=Object.entries(next);
    for(let i=0;i<entries.length;i++)for(let j=0;j<i;j++){
      const [a,first]=entries[i], [b,second]=entries[j];
      const ac=definitions[a].contexts,bc=definitions[b].contexts;
      if(variants(a,first).some(x=>variants(b,second).some(y=>signature(x)===signature(y))) && (ac.includes("*") || bc.includes("*") || ac.some(c=>bc.includes(c))))
        throw new Error(`INPUT_BINDING_CONFLICT: ${a} / ${b}`);
    }
    this.bindings = next; // Publish atomically only after the whole map validates.
  }
  snapshot(): BindingSettings {
    return { version: 1, overrides: Object.fromEntries(Object.entries(this.bindings)
      .map(([key, value]) => [key, { ...value }])) };
  }
  label(action: string): string {
    const binding = this.bindings[action];
    return binding ? bindingLabel(binding) : action;
  }

  displayLabel(action:string):string {
    return variants(action,this.bindings[action]).map(bindingLabel).join(" / ");
  }

  normalize(event: InputEvent, context = ""): InputEvent {
    // Cmd clipboard aliases are a platform convention, not an arbitrary modifier rewrite.
    if (event.key.super && !event.key.meta && !event.key.shift && !event.key.ctrl && ["c","v"].includes(event.input)
        && !Object.entries(this.bindings).some(([action,binding])=>applicable(action,context) && matches(event,binding)))
      event={input:event.input,key:{...event.key,super:false,ctrl:true}};
    // Bracketed paste is literal input, never a shortcut/intent to execute.
    const namedKey = Object.values(INPUT_ACTIONS).some(binding => binding.key.length > 1 && event.key[binding.key]);
    if (event.key.paste || (event.input.length > 1 && !namedKey && !event.key.ctrl && !event.key.meta)) return event;
    for (const [action, physical] of Object.entries(this.bindings)) {
      if (!applicable(action, context) || !variants(action,physical).some(binding=>matches(event,binding))) continue;
      const canonical = INPUT_ACTIONS[action]!;
      // Every focused editor receives newline as literal input. It must never
      // reach a generic Enter handler as a submit/confirm action.
      if (action === "editor.newline") return {input: "\n", key: {newline: true, bindingAction: action}};
      return { input: canonical.key.length === 1 ? canonical.key : "",
        key: { bindingAction: action, ctrl: !!canonical.ctrl, shift: !!canonical.shift, meta: !!canonical.meta,
          ...(canonical.key.length > 1 ? { [canonical.key]: true } : {}) } };
    }
    // A displaced default must not remain an accidental second shortcut.
    if (Object.entries(INPUT_ACTIONS).some(([action,binding]) => applicable(action,context) && variants(action,binding).some(value=>matches(event,value)))) return { input: "", key: {} };
    // Legacy handlers also consume named keys directly. Unbound modifier
    // combinations must not silently execute an unmodified action.
    if (namedKey && (event.key.ctrl || event.key.meta || event.key.shift || event.key.super))
      return {input: "", key: {}};
    return event;
  }
}

export class InputRouter {
  constructor(readonly bindings = new InputBindingRegistry()) {}
  private scopes = new Map<string, InputScope>();
  register(scope: InputScope) {
    if (this.scopes.has(scope.id)) throw new Error(`INPUT_SCOPE_DUPLICATE: ${scope.id}`);
    this.scopes.set(scope.id, scope);
    return () => { if (this.scopes.get(scope.id) === scope) this.scopes.delete(scope.id); };
  }
  dispatch(event: InputEvent): string | null {
    const scopes = [...this.scopes.values()].sort((a, b) => b.priority - a.priority);
    for (const scope of scopes) {
      if (scope.enabled() && scope.handle(scope.raw?.() ? event : this.bindings.normalize(event, scope.context?.() || ""))) return scope.id;
    }
    return null;
  }
}

/** The formal app and its hint labels share one registry, not copied tables. */
export const applicationBindings = new InputBindingRegistry();

/** Scene actions retain their existing semantic handlers after physical remapping. */
export function isBindingAction(action: string, input: string, key: InputKey): boolean {
  if(key.paste)return false;
  return key.bindingAction ? key.bindingAction === action : variants(action,INPUT_ACTIONS[action]).some(binding=>matches({input,key},binding));
}
