// src/input/bindings.ts — single source of truth for key chords.
// Each chord maps a human-readable display label to the predicate that
// recognizes it. Both key resolution (keymap.ts / input/overlays/) and
// hint text (component footers) read from here, so a future /config rebind
// menu only needs to change this one module.

import { applicationBindings } from "./router.js";
export type Chord = {
  label: string;
  match: (input: string, key: any) => boolean;
};

function letter(l: string, label?: string): Chord {
  const u = l.toUpperCase();
  return { label: label ?? u, match: (i: string) => i === l || i === u };
}

export const CHORDS: Record<string, Chord> = {
  // Special keys
  escape:     { label: "Esc",         match: (_i: string, k: any) => k.escape },
  enter:      { label: "Enter",       match: (_i: string, k: any) => k.return },
  enterNoShift:{ label: "Enter",      match: (_i: string, k: any) => k.return && !k.shift },
  shiftEnter: { label: "Shift+Enter", match: (_i: string, k: any) => k.return && k.shift },
  tabAny:     { label: "Tab",         match: (_i: string, k: any) => k.tab },
  tab:        { label: "Tab",         match: (_i: string, k: any) => k.tab && !k.shift },
  shiftTab:   { label: "Shift+Tab",   match: (_i: string, k: any) => k.tab && k.shift },
  backspace:  { label: "Backspace",   match: (_i: string, k: any) => k.backspace },
  delete:     { label: "Del",         match: (_i: string, k: any) => k.delete },
  home:       { label: "Home",        match: (_i: string, k: any) => k.home },
  end:        { label: "End",         match: (_i: string, k: any) => k.end },
  up:         { label: "↑",           match: (_i: string, k: any) => k.upArrow },
  down:       { label: "↓",           match: (_i: string, k: any) => k.downArrow },
  left:       { label: "←",           match: (_i: string, k: any) => k.leftArrow },
  right:      { label: "→",           match: (_i: string, k: any) => k.rightArrow },
  pageUp:     { label: "PgUp",        match: (_i: string, k: any) => k.pageUp },
  pageDown:   { label: "PgDn",        match: (_i: string, k: any) => k.pageDown },
  upDown:     { label: "↑↓",          match: (_i: string, k: any) => k.upArrow || k.downArrow },
  leftRight:  { label: "←/→",         match: (_i: string, k: any) => k.leftArrow || k.rightArrow },

  // Modifier combos
  ctrlLeft:   { label: "Ctrl+←",      match: (_i: string, k: any) => k.ctrl && k.leftArrow },
  ctrlRight:  { label: "Ctrl+→",      match: (_i: string, k: any) => k.ctrl && k.rightArrow },
  altB:       { label: "Alt+B",       match: (i: string, k: any) => k.meta && (i === "b" || i === "B") },
  altF:       { label: "Alt+F",       match: (i: string, k: any) => k.meta && (i === "f" || i === "F") },
  ctrlK:      { label: "Ctrl+K",      match: (i: string, k: any) => k.ctrl && (i === "k" || i === "K") },
  ctrlU:      { label: "Ctrl+U",      match: (i: string, k: any) => k.ctrl && (i === "u" || i === "U") },
  ctrlW:      { label: "Ctrl+W",      match: (i: string, k: any) => k.ctrl && (i === "w" || i === "W") },
  ctrlY:      { label: "Ctrl+Y",      match: (i: string, k: any) => k.ctrl && (i === "y" || i === "Y") },
  ctrlG:      { label: "Ctrl+G",      match: (i: string, k: any) => k.ctrl && (i === "g" || i === "G") },

  // Printable command / shortcut characters
  slash:      { label: "/",           match: (i: string) => i === "/" },

  // Letter action keys
  letterL: letter("l"),
  letterH: letter("h"),
  letterR: letter("r"),
  letterD: letter("d"),
  shiftD:   { label: "Shift+D", match: (i: string, k: any) => k.shift && (i === "d" || i === "D") },
  letterX: letter("x"),
  letterS: letter("s"),
  letterV: letter("v"),
  letterA: letter("a"),
  letterP: letter("p"),
  letterN: letter("n"),
  letterE: letter("e"),
  letterT: letter("t"),
};

export function matchChord(name: string, input: string, key: any): boolean {
  if (["left", "right", "up", "down", "upDown", "leftRight"].includes(name) && (key.ctrl || key.meta)) return false;
  const chord = CHORDS[name];
  return chord ? chord.match(input, key) : false;
}

export function chordLabel(name: string): string {
  const actions: Record<string, string> = {escape: "execution.interrupt", enter: "navigation.confirm", enterNoShift: "navigation.confirm", shiftEnter: "editor.newline", tab: "navigation.tab", tabAny: "navigation.tab", shiftTab: "navigation.previousTab", left: "navigation.left", right: "navigation.right", up: "navigation.up", down: "navigation.down", pageUp: "viewport.pageUp", pageDown: "viewport.pageDown", ctrlLeft: "editor.previousWord", ctrlRight: "editor.nextWord", ctrlK: "editor.killToEnd", ctrlU: "editor.killToStart", ctrlW: "editor.killWord", ctrlY: "editor.yank", ctrlG: "editor.external", home: "editor.start", end: "editor.end", delete: "editor.delete", backspace: "editor.backspace"};
  if (actions[name]) return applicationBindings.label(actions[name]);
  if (name === "upDown") return chordLabel("up") + chordLabel("down");
  if (name === "leftRight") return chordLabel("left") + "/" + chordLabel("right");
  const chord = CHORDS[name];
  return chord ? chord.label : name;
}
