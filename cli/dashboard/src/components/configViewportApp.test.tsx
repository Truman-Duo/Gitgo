import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";
import {BINDING_DEFINITIONS, applicationBindings} from "../input/router.js";
import type {BackendClient} from "../backend/client.js";

// Complete App composition and production input decoder, with a local backend fixture.
// Rendered-frame assertions do not replace visual acceptance in a real terminal.
test("config keeps one bounded viewport through held navigation, resizing and tab changes", async () => {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin; stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = true; stdout.columns = 100; stdout.rows = 24; stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const client: BackendClient = {ready: true, close() {}, async callTool(operation) {
    if (operation === "project.overview") return {projects: [{name: "ready", workspace: "C:/ready", runtime_summary: {state_available: true}}]};
    if (operation === "config.get") return {language: "en", keyboard_bindings: {version: 1, overrides: {}}};
    return {};
  }};
  const root = renderSync(<InputProvider><App client={client}/></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const ink = instances.get(stdout) as any;
  const wait = () => new Promise(resolve => setTimeout(resolve, 90));
  const key = async (value: string) => {stdin.write(value); await wait();};
  const scrolls = (node: any): any[] => [
    ...(node.scrollViewportHeight !== undefined ? [node] : []),
    ...(node.childNodes || []).flatMap(scrolls),
  ];
  const frame = () => {
    const screen = ink.frontFrame.screen;
    return Array.from({length: screen.height}, (_, y) =>
      Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || "").join("")).join("\n");
  };
  const bottom = (node: any) => Math.max(0, node.scrollHeight - node.scrollViewportHeight);
  try {
    await wait();
    await key("/"); await key("config keybindings"); await key("\r"); await key("\r");
    expect(frame()).toContain("Restore all defaults");
    const viewport = scrolls(ink.rootNode).at(-1);
    const last = BINDING_DEFINITIONS.at(-1)!;
    const lastLabel = `${last.label.en} · ${applicationBindings.displayLabel(last.id)}`;
    const header = frame().split("\n").findIndex(line => line.includes("General") && line.includes("Providers"));
    await key("\x1b[B".repeat(100));
    expect(frame()).toContain(lastLabel);
    expect(scrolls(ink.rootNode)).toHaveLength(1);
    expect(viewport.scrollHeight).toBeGreaterThan(viewport.scrollViewportHeight);
    expect(viewport.scrollTop).toBe(bottom(viewport));
    const original = ink.rootNode.onRender;
    let schedules = 0;
    ink.rootNode.onRender = (...args: any[]) => {schedules++; return original?.(...args);};
    try {
      for (let i = 0; i < 10; i++) await key("\x1b[B".repeat(100));
      expect(viewport.scrollTop).toBe(bottom(viewport));
      expect(viewport.pendingScrollDelta || 0).toBe(0);
      expect(schedules).toBeLessThanOrEqual(1);
    } finally {ink.rootNode.onRender = original;}
    expect(frame().split("\n")[header]).toContain("Keybindings");
    await key("\x1b[A");
    expect(frame()).toContain(BINDING_DEFINITIONS.at(-2)!.label.en);
    for (const [width, height] of [[70, 16], [46, 10], [120, 30]]) {
      stdout.columns = width; stdout.rows = height; stdout.emit("resize"); await wait(); await wait();
      await key("\x1b[B");
      expect(frame()).toContain(last.label.en);
      expect(frame()).toContain("Test keyboard input");
      expect(frame()).toContain("Restore all defaults");
      expect(frame()).toContain("Esc back");
      expect(viewport.scrollTop).toBe(bottom(viewport));
      expect(viewport.pendingScrollDelta || 0).toBe(0);
    }
    await key("\x1b[A".repeat(100)); expect(viewport.scrollTop).toBe(0);
    // Keep legacy long-form tabs scrollable while the config shell/header stays fixed.
    await key("\x1b"); await key("\x1b[D"); await key("\x1b[D"); await key("\r");
    expect(scrolls(ink.rootNode)).toHaveLength(1);
    stdout.rows = 12; stdout.emit("resize"); await wait(); await wait();
    const generalViewport = scrolls(ink.rootNode)[0];
    expect(bottom(generalViewport)).toBeGreaterThan(0);
    await key("\x1b[6~");
    expect(generalViewport.scrollTop).toBeGreaterThan(0);
    expect(frame()).toContain("General"); expect(frame()).toContain("Providers");
    await key("\x1b"); await key("\x1b");
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
}, 15000);
