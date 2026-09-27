/** Terminal input control plane. One event, one owner; views never read stdin. */
export type InputKey = Record<string, any>;
export type InputEvent = { input: string; key: InputKey };
export type InputScope = {
  id: string;
  priority: number;
  enabled: () => boolean;
  handle: (event: InputEvent) => boolean;
};

export type BindingSpec = { key: string; ctrl?: boolean; shift?: boolean; meta?: boolean };
export type BindingSettings = { version: 1; overrides: Record<string, BindingSpec> };

/** Serializable action bindings, ready for a future config editor.
 * The canonical events keep existing scene-specific command resolvers compatible.
 */
export const INPUT_ACTIONS: Record<string, BindingSpec> = {
  "execution.interrupt": { key: "escape" },
  "navigation.confirm": { key: "return" },
  "editor.newline": { key: "return", shift: true },
  "navigation.tab": { key: "tab" },
  "navigation.previousTab": { key: "tab", shift: true },
  "navigation.up": { key: "upArrow" },
  "navigation.down": { key: "downArrow" },
  "navigation.left": { key: "leftArrow" },
  "navigation.right": { key: "rightArrow" },
  "viewport.pageUp": { key: "pageUp" },
  "viewport.pageDown": { key: "pageDown" },
  "clipboard.copy": { key: "c", ctrl: true },
  "clipboard.paste": { key: "v", ctrl: true },
  "editor.backspace": { key: "backspace" },
  "editor.delete": { key: "delete" },
  "editor.start": { key: "home" },
  "editor.end": { key: "end" },
  "editor.previousWord": { key: "leftArrow", ctrl: true },
  "editor.nextWord": { key: "rightArrow", ctrl: true },
  "editor.killToEnd": { key: "k", ctrl: true },
  "editor.killToStart": { key: "u", ctrl: true },
  "editor.killWord": { key: "w", ctrl: true },
  "editor.yank": { key: "y", ctrl: true },
  "editor.external": { key: "g", ctrl: true },
};

function signature(binding: BindingSpec) {
  return `${binding.key.toLowerCase()}:${+!!binding.ctrl}:${+!!binding.shift}:${+!!binding.meta}`;
}
function matches(event: InputEvent, binding: BindingSpec) {
  return (binding.key.length === 1
    ? event.input.toLowerCase() === binding.key.toLowerCase()
    : Boolean(event.key[binding.key]))
    && !!event.key.ctrl === !!binding.ctrl && !!event.key.shift === !!binding.shift
    && !!(event.key.meta && !event.key.escape) === !!binding.meta;
}

export class InputBindingRegistry {
  private bindings = { ...INPUT_ACTIONS };
  configure(settings: BindingSettings) {
    if (settings.version !== 1) throw new Error("INPUT_BINDING_VERSION_UNSUPPORTED");
    const next = { ...INPUT_ACTIONS };
    for (const [action, binding] of Object.entries(settings.overrides)) {
      if (!Object.hasOwn(INPUT_ACTIONS, action) || !binding || typeof binding.key !== "string" || !binding.key ||
          [binding.ctrl, binding.shift, binding.meta].some(value => value !== undefined && typeof value !== "boolean") ||
          !(binding.key.length === 1 || Object.values(INPUT_ACTIONS).some(item => item.key === binding.key))) {
        throw new Error(`INPUT_BINDING_INVALID: ${action}`);
      }
      next[action] = { ...binding };
    }
    const occupied = new Set<string>();
    for (const value of Object.values(next)) {
      const sig = signature(value);
      if (occupied.has(sig)) throw new Error(`INPUT_BINDING_CONFLICT: ${sig}`);
      occupied.add(sig);
    }
    this.bindings = next; // Publish atomically only after the whole map validates.
  }
  snapshot(): BindingSettings {
    return { version: 1, overrides: Object.fromEntries(Object.entries(this.bindings)
      .map(([key, value]) => [key, { ...value }])) };
  }
  label(action: string): string {
    const binding = this.bindings[action];
    if (!binding) return action;
    const names: Record<string, string> = {escape: "Esc", return: "Enter", tab: "Tab", upArrow: "↑", downArrow: "↓", leftArrow: "←", rightArrow: "→", pageUp: "PgUp", pageDown: "PgDn", backspace: "Backspace", delete: "Del", home: "Home", end: "End"};
    return [binding.ctrl ? "Ctrl" : "", binding.shift ? "Shift" : "", binding.meta ? "Alt" : "", names[binding.key] || binding.key.toUpperCase()].filter(Boolean).join("+");
  }
  normalize(event: InputEvent): InputEvent {
    // Bracketed paste is literal input, never a shortcut/intent to execute.
    if (event.input.length > 1 && !event.key.ctrl && !event.key.meta) return event;
    for (const [action, physical] of Object.entries(this.bindings)) {
      if (!matches(event, physical)) continue;
      const canonical = INPUT_ACTIONS[action]!;
      return { input: canonical.key.length === 1 ? canonical.key : "",
        key: { ctrl: !!canonical.ctrl, shift: !!canonical.shift, meta: !!canonical.meta,
          ...(canonical.key.length > 1 ? { [canonical.key]: true } : {}) } };
    }
    // A displaced default must not remain an accidental second shortcut.
    if (Object.values(INPUT_ACTIONS).some(binding => matches(event, binding))) return { input: "", key: {} };
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
    const normalized = this.bindings.normalize(event);
    const scopes = [...this.scopes.values()].sort((a, b) => b.priority - a.priority);
    for (const scope of scopes) {
      if (scope.enabled() && scope.handle(normalized)) return scope.id;
    }
    return null;
  }
}

/** The formal app and its hint labels share one registry, not copied tables. */
export const applicationBindings = new InputBindingRegistry();
