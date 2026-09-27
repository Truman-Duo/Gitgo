export const PASTE_THRESHOLD = 800;
export const PASTE_LINE_THRESHOLD = 2;
const PASTE_REF_RE = /\[Pasted text #(\d+) \+(\d+) lines\]/g;

/** Session-draft paste objects. Submitted content is expanded before it enters
 * the backend, where normal session/CAS persistence takes over. */
export class PromptPasteStore {
  private sequence = 0;
  private values = new Map<number, string>();

  collapse(text: string, pasted = false): string {
    if (!pasted) return text;
    const lines = text.split(/\r?\n/).length;
    if (text.length <= PASTE_THRESHOLD && lines <= PASTE_LINE_THRESHOLD) return text;
    this.sequence += 1;
    this.values.set(this.sequence, text);
    return `[Pasted text #${this.sequence} +${lines} lines]`;
  }

  materialize(value: string): string {
    return value.replace(PASTE_REF_RE, (whole, rawId) => this.values.get(Number(rawId)) ?? whole);
  }

  recollapse(value: string): string {
    let result = value;
    for (const [id, content] of this.values) {
      const index = result.indexOf(content);
      if (index < 0) continue;
      const lines = content.split(/\r?\n/).length;
      result = result.slice(0, index) + `[Pasted text #${id} +${lines} lines]`
        + result.slice(index + content.length);
    }
    return result;
  }

  clear(): void { this.values.clear(); }
}
