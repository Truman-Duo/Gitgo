import type {InputKey} from "../input/router.js";

export type Language = "en" | "zh";
export type Copy = Readonly<Record<Language, string>>;
export type Element =
 | {kind: "text"; rich: boolean}
 | {kind: "header"; levels: 1 | 2; divider:"after"}
 | {kind: "list"; axis: "vertical" | "horizontal"; selectable: boolean}
 | {kind: "chart"; chart: "heatmap" | "waterfall" | "context"}
 | {kind: "editor"; multiline: boolean}
 | {kind: "actions"; axis: "vertical" | "horizontal"};
export type PageDefinition<Id extends string = string> = {
 id: Id; title: Copy; layout: "viewport" | "drawer"; elements: readonly Element[];
 navigation: "header-content-actions" | "rows" | "discussion";
 spacing: {paddingX: 1; paddingTop: 1}; motion: "none";
};
export function definePage<const Id extends string>(page: PageDefinition<Id>): PageDefinition<Id> {
 if (!page.id || !page.elements.length || !page.title.en.trim() || !page.title.zh.trim())
  throw Error("PAGE_CONTRACT_INVALID: identity, elements and both translations are required");
 if (page.navigation === "header-content-actions" && !page.elements.some(e => e.kind === "header"))
  throw Error("PAGE_CONTRACT_INVALID: header navigation requires a header element");
 return Object.freeze(page);
}
export function copy(value: Copy, language: Language): string {
 const text = value[language];
 if (!text?.trim()) throw Error("UI_TRANSLATION_MISSING: "+language);
 return text;
}
export type Selectable<Id extends string | number = string> = {id: Id; enabled: boolean};
export function reconcileSelection<Id extends string | number>(items: readonly Selectable<Id>[], selected: Id): Id | undefined {
 return items.find(item => item.id === selected && item.enabled)?.id ?? items.find(item => item.enabled)?.id;
}
export function moveSelection<Id extends string | number>(items: readonly Selectable<Id>[], selected: Id, delta: number): Id | undefined {
 const enabled = items.filter(item => item.enabled);
 if (!enabled.length) return undefined;
 const at = enabled.findIndex(item => item.id === selected);
 return enabled[Math.max(0,Math.min(enabled.length-1,at < 0 ? 0 : at + delta))]?.id;
}
export type StandardKey = "back" | "activate" | "up" | "down" | "left" | "right" | "next" | "pageUp" | "pageDown";
export function standardKey(input: string, key: InputKey): StandardKey | undefined {
 if (key.paste || key.ctrl || key.meta) return;
 if (key.escape) return "back";
 if (key.return) return "activate";
 if (key.upArrow) return "up";
 if (key.downArrow) return "down";
 if (key.leftArrow) return "left";
 if (key.rightArrow) return "right";
 if (key.tab) return "next";
 if (key.pageUp || key.wheelUp) return "pageUp";
 if (key.pageDown || key.wheelDown) return "pageDown";
 // Printable letters are never implicit actions in standard pages.
}
const common = {spacing: {paddingX: 1, paddingTop: 1}, motion: "none"} as const;
export const pages = {
 processDetail: definePage({...common,id:"processDetail",title:{en:"Subprocess details",zh:"子进程详情"},layout:"viewport",
  navigation:"rows",elements:[{kind:"text",rich:true}]}),
 help: definePage({...common,id:"help",title:{en:"Help",zh:"帮助"},layout:"viewport",
  navigation:"header-content-actions",elements:[{kind:"header",levels:1,divider:"after"},{kind:"text",rich:true}]}),
 lessons: definePage({...common,id:"lessons",title:{en:"Lessons",zh:"经验"},layout:"viewport",
  navigation:"rows",elements:[{kind:"text",rich:true},{kind:"list",axis:"vertical",selectable:true},{kind:"editor",multiline:true},{kind:"actions",axis:"horizontal"}]}),
 trace: definePage({...common,id:"trace",title:{en:"Turn waterfall",zh:"逐轮瀑布图"},layout:"viewport",
  navigation:"rows",elements:[{kind:"chart",chart:"waterfall"},{kind:"actions",axis:"horizontal"}]}),
 usage: definePage({...common,id:"usage",title:{en:"Usage overview",zh:"用量总览"},layout:"viewport",
  navigation:"header-content-actions",elements:[{kind:"header",levels:1,divider:"after"},{kind:"chart",chart:"heatmap"},{kind:"list",axis:"vertical",selectable:true},{kind:"actions",axis:"horizontal"}]}),
 update: definePage({...common,id:"update",title:{en:"Gitgo Software Update",zh:"Gitgo 软件更新"},layout:"viewport",
  navigation:"rows",elements:[{kind:"text",rich:false},{kind:"actions",axis:"vertical"}]}),
 runtime: definePage({...common,id:"runtime",title:{en:"Runtime",zh:"运行信息"},layout:"viewport",
  navigation:"header-content-actions",elements:[{kind:"header",levels:2,divider:"after"},{kind:"text",rich:true},{kind:"list",axis:"vertical",selectable:true},{kind:"chart",chart:"context"}]}),
 discussion: definePage({...common,id:"discussion",title:{en:"Side discussion",zh:"旁路讨论"},layout:"drawer",
  navigation:"discussion",elements:[{kind:"list",axis:"horizontal",selectable:true},{kind:"text",rich:true},{kind:"editor",multiline:true},{kind:"actions",axis:"horizontal"}]}),
} as const;
