import {keyHintText} from "../input/hints.js";
import type {InputKey} from "../input/router.js";
import {geometry} from "../theme/tokens.js";

export type Language = "en" | "zh";
export type Copy = Readonly<Record<Language, string> & {keysResolved?:boolean;keys?:boolean}>;
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
 return value.keys && !value.keysResolved ? keyHintText(text) : text;
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
export type StandardKey = "back" | "activate" | "up" | "down" | "left" | "right" | "next" | "previous" | "pageUp" | "pageDown";
export function standardKey(input: string, key: InputKey): StandardKey | undefined {
 if (key.paste || key.ctrl || key.meta) return;
 if (key.escape) return "back";
 if (key.return) return "activate";
 if (key.upArrow) return "up";
 if (key.downArrow) return "down";
 if (key.leftArrow) return "left";
 if (key.rightArrow) return "right";
 if (key.tab) return key.shift ? "previous" : "next";
 if (key.pageUp || key.wheelUp) return "pageUp";
 if (key.pageDown || key.wheelDown) return "pageDown";
 // Printable letters are never implicit actions in standard pages.
}
/** Element contracts used by field/feedback components, independent of page navigation. */
export const elementStandards = {
  pageFrame: {slots:["header","content","footer"],chrome:"intrinsic-height-no-shrink",content:"bounded-flex-viewport"},
  textCaret:{components:['TextInput','useDeclaredCursor'],style:'native-blinking-bar',empty:'visible-at-placeholder-origin',
    owner:'active-declaration;render-after-frame-patches',focus:'blur-hides;document-offscreen-hides-without-losing-IME-position',
    cadence:'terminal-owned-blink;no-React-blink-timer'},
  questionGroup:{components:['DecisionPanel','DecisionSummary','decisionRound'],source:'canonical-current-task-decision-identities',
    questions:'dependency-ready-independent-facets;each-with-advice;no-position-counters',
    input:'existing-scene-owner;left-right-selects-when-not-editing;enter-submits-one-real-reply-immediately',
    lifecycle:'received-subset-resumes-semantic-recomputation;no-local-answer-queue;reject-keeps-current-card',
    history:'show-only-actually-answered-facets;full-original-request-stays-in-ledger',
    compatibility:'legacy-saved-rounds-retain-original-complete-answer-protocol'},
  disclosure: {component:"Disclosure",state:"controlled-per-record",selection:"SelectionText",input:"caller-owned",motion:"AnimatedReveal",empty:"no-fake-toggle",
    activityMarker:"one-directional-marker;shared-clock-yellow-pulse-while-running;static-while-waiting-or-terminal",
    hierarchy:"contentInset-and-theme-record-indent",resize:"open-async-content-flows-without-restarting-reveal",
    click:"captured-action-owner;one-cell-tremor;release-on-same-live-owner;drag-cancels",
    updates:"functional-toggles-preserve-batched-clicks",reading:"one-geometry-commit;post-commit-action-anchor;no-height-animation-in-virtual-reader",
    persistence:"conversation-owned-scoped-record-identity;survives-reflow-and-virtual-eviction"},
  traceContent: {component:"TraceContent",source:"original-snapshot-bound-trace-or-saved-detail",loading:"visible-or-expanded-only",
    input:"caller-owned",pagination:"automatic-bounded-pages",staleAsyncDelivery:"discard",errors:"useFeedback-timed"},
  virtualContent: {components:["ContentViewport","VirtualContent"],viewport:"existing-ScrollBox",input:"none",
    clampOwner:"outer-viewport-only",measurement:"stable-key-and-width;skip-nonpositive-hidden-geometry",content:"all-rows-scrollable",
    following:"explicit-live-or-bottom-intent;short-readonly-growth-preserves-start",
    lifecycle:"subscribe-to-current-viewport-handle-after-each-commit;detach-replaced-owner",
    geometry:"painted-metrics-reconcile-child-local-growth;changed-clamp-invalidates-paint"},
  viewportScrollbar:{component:"ViewportScrollbar",width:1,theme:"neutral-chat-tokens",input:"track-press-seeks;captured-thumb-drag-preserves-grab-offset;release-or-blur-ends-gesture;original-wheel-and-keyboard-owner",
    metrics:"actual-rendered-scroll-top;coalesced-post-paint;no-polling;no-reader-rerender-on-thumb-only-update",overflow:"full-height-neutral-thumb-when-content-fits",
    visibility:"initially-hidden;right-gutter-hover-or-press-reveals;body-hover-and-scroll-do-not-reveal;gutter-leave-hides-after-3000ms;drag-pins-visible;one-gutter-always-reserved",
    scope:"main-and-subprocess-conversations;initial-history-at-latest;manual-reading-wins",layout:'stretch-inside-parent-content-width;no-outer-percentage-after-padding'},
  discussionReader:{component:'BtwPanel',initialFocus:'discussion-body',focus:'main-click-selects-parent;discussion-click-selects-body;editor-click-selects-editor',
    boundaries:'no-automatic-parent-handoff',authority:'existing-input-router-and-scroll-handles;no-task-or-permission-change'},
  presentationClock: {hook:"useSharedClock",timers:"one-per-active-theme-period",idle:"stop-with-last-subscriber",
    responsibility:"presentation-only-not-runtime-deadlines"},
  detailViewport:{component:"DetailViewport",maximumRows:geometry.record.detailMaxRows,
    contextMaximumRows:geometry.record.contextMaxRows,thoughtMaximumRows:geometry.record.thoughtMaxRows,
    frame:"single-neutral-divider;terminal-background;existing-decision-frame-reused",
    width:"measured-parent-minus-frame;render-body-with-actual-inner-width",
    input:"existing-router;read-only-hover-or-click-owner;active-decisions-keep-original-owner",
    wheel:"painted-clipped-bounds;child-first;remaining-delta-to-parent",keyboard:"arrows-pages-home-end;escape-closes-one-level",
    body:"virtual-original-text;visible-pages-only;no-extra-arguments-or-body-menu"},
  header: {activation:"enter-only",arrows:"select-without-entering",tab:"never-enter-from-header"},
  footerDrawer: {component:"FooterDrawer",placement:"between-editor-and-status",maximum:geometry.drawer.maxHeightRatio,
    viewport:"shared-scroll-or-explicit-self-owned",input:"preserve-editor-no-duplicate",businessState:"caller-owned"},
  searchableChoice: {component:"SearchableChoiceScene",presentations:["content","inline"],row:"SelectionRow-or-SuggestionRow",counter:"none"},
  scale: {component:"DiscreteScale",navigation:"bounded",color:"triangle-only",state:"controlled"},
  actions: {component:"PageActions",selection:"SelectionText-block",gap:geometry.actions.gap,authorization:"caller-owned"},
  taskExecutionRules: {components:["TaskMechanismsPanel","DecisionPanel"],source:"committed-user-task-policy",
    authoring:"main-conversation-and-version-bound-confirmation",management:"enable-disable-delete-export-by-task-and-version"},
  help: {component:"ModuleHelpBoundary",input:"InputSuspension",state:"retain-original",footer:"restore-original"},
  turnGuide: {component:"TurnGuide",geometry:"theme-single-line",color:"theme-state-aware",endpoints:"caller-owned-turn-state"},
  publicProgress: {components:["TurnTimeline","MessageRow"],source:"explicit-phase-or-agent-update",
    identity:"task-process-update-or-original-trace-sequence",history:"ledger-or-lazy-original-trace-index",
    visibility:"inside-round-capsule",bodies:"plain-text-without-progress-label-or-nested-disclosure",missing:"never-synthesize"},
  roundCapsule:{components:["VirtualConversation","MessageRow","StreamingMessage"],terminal:"user-and-answered-input-summaries-capsule-answer",
    default:"folded-after-completion-and-on-history-entry",process:"all-progress-tools-notices-and-historical-decisions",
    thinking:"current-provider-phase-only;compact-status-not-expandable;no-injection-time-or-history",
    finalAnswer:"one-authoritative-outcome-outside",verbose:"inside-only",refresh:"preserve-user-toggle",
    inputs:"task-records-exclude-retained-model-dialogue;legacy-replays-require-archived-original-identity"},
  publishWorkspace:{component:"PublishWorkbench",entry:"workspace-header",return:"mounted-caller-state",
    navigation:"bounded-existing-input-owner",viewPersistence:"coalesced-and-flushed-on-unmount",
    graphics:"reuse-unchanged-raster-no-empty-scene",repositoryTarget:"same-versioned-configuration-as-account-settings"},
  projectProcessCount:{component:"Overview",source:"observed-owned-execution-not-durable-running-labels",
    reconciliation:"newer-overview-replaces-older-events;inflight-new-events-win",color:"neutral-path-style"},
  terminalNotice:{component:"RuntimeNoticeRow",kinds:["agent_complete","agent_terminal"],presentation:"plain-status-no-marker-or-disclosure",
    failure:"retain-saved-detail-inside-round"},
  requestContext:{component:"RequestContextRow",source:"saved-dispatch-snapshot-only",default:"folded",
    compact:"hidden-no-read-no-thinking-child",verbose:"independent-inside-round",progress:"never",
    read:"metadata-then-visible-8192-character-pages",budget:"two-coalesced-observation-reads-per-client",
    body:"unframed-virtual-source-list;each-open-source-has-own-framed-16-row-DetailViewport",
    disclosure:"source-selection-owned-above-virtual-rows;reference-scoped",loading:"reserve-estimated-page-height"},
  stageReport:{component:"StageReport",report:"public-prose-unbounded-and-always-visible",
    detail:"same-process-provider-cycle-only;verbose-only;16-row-unframed-viewport",
    toggle:"leading-arrow-controls-detail-only",guide:"vertical-with-bottom-L-no-top-fork",
    lifecycle:"transient-cycle;retire-on-next-request-wait-terminal-or-recovery;no-history-thought"},
  decisionSummary:{components:["DecisionSummary","DecisionPanel"],source:"same-task-process-decision-and-recorded-answer",
    summary:"outside-round-single-line-question-reply",detail:"same-inline-header-then-full-question-reply-options",
    answer:"unwrap-matching-original-option-envelope-retain-free-text-and-conditions",layout:"parent-content-width-including-gutter",
    updates:"memoize-unchanged-question-reply-preserve-language-and-expansion",authority:"read-only-no-second-submit"},
  toolCall:{component:"ToolCallDisplay",normalName:"theme-gray-no-success-label",failedName:"theme-danger-no-row-fill",
    header:"name-and-concise-source-arguments-on-one-line",body:"direct-original-content-or-result-and-diff",
    folded:"header-only-no-body-read",default:"folded-independent-of-verbose",group:"expand-to-real-calls",
    contextPreparation:"context-source-only;never-an-executed-tool",
    counts:"natural-words-search-requests-fetch-pages-mixed-calls"},
  selection: {components: ["SelectionRow", "SelectionText", "HorizontalHeaderStrip"], leadingMarker: false, positionCounter: false,
    weight: "selected-independent-of-focus", color: "theme-only", motion: "immediate"},
  tableHeader: {component: "TableHeaderText", weight: "always-bold"},
  field: {labelWidth: "terminal-cells", editIndicator: "blink-label-only", leadingMarker: false,
    weight: "selected-only", textEditor: "normal-bar-only-while-editing"},
  choice: {presentation: "current-value", keys: "left-right", navigation: "cycle", confirmation: "enter",labels:"localized-separate-from-stored-values"},
  releaseDetails:{components:["SelectionRow","SearchableChoiceScene","PageActions","usePublishFormEditor","ScrollBox"],
    phases:"existing-version-detail-no-extra-tabs",sourceRead:"explicit-selection-only",secrets:"masked-no-draft-or-command-receipt",
    confirmation:"caller-owned-phase-and-target",capabilities:"adapter-and-project-recipe"},
  feedback: {owner: "explicit-page-project-or-global", defaultRetention: "timed",
    warningAndErrorRetention: "always-timed", persistent: "informational-unresolved-operation-or-state",
    staleAsyncDelivery: "discard-including-return-to-same-owner"},
  language: {source: "bilingual-copy", externalContent: "preserve-original"},
} as const;
const common = {spacing: geometry.page, motion: "none"} as const;
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
