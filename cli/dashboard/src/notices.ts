// src/notices.ts — centralized notice/error dispatch.
// Single source of truth for transient user-facing feedback. Every notice carries
// a four-digit code rendered as "(XXXX) message". `noticeToActions` translates a
// code (+ params) into the AppActions that realize the notice — a toast alone, or
// a toast that also links other components (refresh / navigate / panel).
//
// Pure, no React.

import type { AppAction, OverlayType, Scene } from "./state/store.js";

export type NoticeKind = "toast" | "refresh" | "navigate" | "panel";

type NoticeDef = {
  code: number;
  kind: NoticeKind;
  message: string; // may contain {param} placeholders
};

export const NOTICES: Record<number, NoticeDef> = {
  // 1xxx — command resolution
  1001: { code: 1001, kind: "toast", message: "Unknown command: {cmd}" },
  1002: { code: 1002, kind: "toast", message: "Only available in {scene}" },

  // 2xxx — process / interrupt
  2001: { code: 2001, kind: "toast", message: "No running agent to interrupt" },
  2002: { code: 2002, kind: "toast", message: "Interrupt failed: {reason}" },
  2003: { code: 2003, kind: "toast", message: "Interrupted agent {pid}" },

  // 3xxx — project selection
  3001: { code: 3001, kind: "toast", message: "No project selected" },
  3002: { code: 3002, kind: "toast", message: "No project selected for export" },

  // 4xxx — backend / generic
  4001: { code: 4001, kind: "toast", message: "Error: {reason}" },
};

export type NoticeParams = Record<string, any>;

/** Render "(XXXX) message" with placeholders interpolated. */
export function formatNotice(code: number, params?: NoticeParams): string {
  const def = NOTICES[code];
  const raw = def ? def.message : `Unknown notice code ${code}`;
  const message = raw.replace(/\{(\w+)\}/g, (_, key: string) => {
    const v = params?.[key];
    return v === undefined || v === null ? `{${key}}` : String(v);
  });
  return `(${code}) ${message}`;
}

/** Translate a notice into the AppActions that realize it. */
export function noticeToActions(code: number, params?: NoticeParams): AppAction[] {
  const def = NOTICES[code];
  const acts: AppAction[] = [{ type: "set_cmd_result", text: formatNotice(code, params) }];

  const kind: NoticeKind = def?.kind ?? "toast";
  switch (kind) {
    case "refresh":
      acts.push({ type: "bump_refresh_key" });
      break;
    case "navigate":
      if (params?.scene) acts.push({ type: "navigate", scene: params.scene as Scene });
      break;
    case "panel":
      if (params?.overlay) {
        acts.push({
          type: "push_overlay",
          overlay: params.overlay as OverlayType,
          props: params.props,
        });
      }
      break;
    case "toast":
      break;
  }

  return acts;
}
