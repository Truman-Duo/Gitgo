import {copy,type Copy} from "../ui/pageContract.js";
// src/components/CommandBar.tsx — v7: FooterConfig injection for overlays.
// chat scenes (workspace, agent_detail) → NORMAL box for typing, / to command.
// non-chat scenes (projects, process_list) → no NORMAL box,
//   / pops COMMAND box, Esc dismisses it.
// Overlays inject a FooterConfig to override the footer; CommandBar renders
// the overlay's cmdInput/suggestions/status instead of the default scene props.
import React, { memo, useState, useEffect } from "react";
import { Box, Text, Ansi } from "@anthropic/ink";
import { TextInput } from "./TextInput.js";
import {SuggestionRow} from "./Selection.js";
import { isChatScene, type Scene, type Mode } from "../state/store.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import { colors, useColorTransition, useSuggestionStyle } from "../theme/index.js";
import { ContextStatus } from "./ContextStatus.js";
import { RunningBStrip } from "./RunningBStrip.js";
import {geometry} from "../theme/tokens.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";

export type Suggestion = {
  label: string;
  description: string;
  inputMode?: "execute" | "fill";
};

/** Overlay-provided footer configuration. */
export type FooterConfig =
  | { hidden: true }
  | {
      hidden?: false;
      kind?: "command" | "normal";
      secret?: boolean;
      preserveStatus?: boolean;
      readOnly?: boolean;
      cmdInput: UseTextInputReturn;
      statusText: string;
      suggestions: Suggestion[];
      suggestionIdx: number;
      cmdResult: string;
    };

type Props = {
  width: number;
  mode: Mode;
  advancement?: "auto" | "manual" | null;
  advancementPending?: boolean;
  textInput: UseTextInputReturn;
  cmdInput: UseTextInputReturn;
  cmdResult: string;
  statusText: string;
  suggestions: Suggestion[];
  suggestionIdx: number;
  scene: Scene;
  footerOverride?: FooterConfig | null;
  runningB?: ProcessInfo[];
  runningBSelIdx?: number;
  statusBarFocused?: boolean;
  contextPct?: string;
  cachePct?: number | null;
  footerDrawer?: React.ReactNode;
  editorHidden?: boolean;
  editorFocused?: boolean;
  language?: "en" | "zh";
};

export const CommandBar = memo(function CommandBar({
  width, mode, advancement, advancementPending = false, textInput, cmdInput, cmdResult,
  statusText, suggestions, suggestionIdx, scene, footerOverride,
  runningB, runningBSelIdx, statusBarFocused, contextPct, cachePct,
  footerDrawer, editorHidden = false, editorFocused = true,
  language = "en",
}: Props) {
  const t=(value:Copy)=>copy(value,language);
  const isOverridden = !!(footerOverride && !footerOverride.hidden);
  const overrideKind = isOverridden ? (footerOverride.kind ?? "command") : "command";

  // Overrides may be a COMMAND bar (green, / prompt) or a NORMAL bar
  // (blue, shared normal prompt) via the `kind` field.
  const isCommand = isOverridden ? overrideKind === "command" : mode === "COMMAND";
  const effectiveCommand = isOverridden ? overrideKind === "command" : (isCommand || !isChatScene(scene));
  const explicitCommand = isOverridden ? false : (isCommand && isChatScene(scene));

  // Use overlay values when overridden
  const activeCmdInput = isOverridden ? footerOverride.cmdInput : cmdInput;
  const activeCmdResult = isOverridden ? footerOverride.cmdResult : cmdResult;
  const sceneStatus = !isOverridden || footerOverride.preserveStatus;
  const activeStatusText = sceneStatus ? statusText : footerOverride.statusText;
  const activeSuggestions: Suggestion[] = isOverridden
    ? footerOverride.suggestions
    : suggestions;
  const activeSuggestionIdx = isOverridden ? footerOverride.suggestionIdx : suggestionIdx;

  // Non-chat scenes: suggestions only when user has typed a leading /
  const nonChatScene = !isChatScene(scene);
  const showSuggestions = effectiveCommand && activeSuggestions.length > 0
    && (nonChatScene ? activeCmdInput.value.startsWith("/") : true);
  // Project/process scenes are list-first: the command editor appears only
  // after a leading slash. Chat scenes keep their NORMAL editor visible.
  const projectListIdle = !isOverridden && (scene === "projects" || scene === "process_list") && !activeCmdInput.value.startsWith("/");
  const showInput = !editorHidden && (isOverridden || isChatScene(scene) || scene === "projects" || scene === "process_list" || activeCmdInput.value.startsWith("/"));

  // Border color animation on mode switch
  const effectiveColor = useColorTransition(
    effectiveCommand,
    colors.input.normal.border,
    colors.input.command.border,
  );
  const contextValue = contextPct || "";

  // Footer override hidden → render nothing (after all hooks to keep order stable).
  if (footerOverride && footerOverride.hidden) {
    return <Box flexDirection="column" flexShrink={0} />;
  }

  // Available width for text wrapping inside the input area.
  const outputWidth = Math.max(30, width);
  // Two horizontal padding cells plus the visible two-cell prompt are outside
  // TextInput's wrap width. Keeping the renderer and cursor on the same width
  // prevents cumulative drift on long CJK/emoji input.
  const horizontalPadding = 2;
  const promptWidth = effectiveCommand && !explicitCommand ? 0 : 2;
  const inputMaxWidth = Math.max(20, outputWidth - horizontalPadding - promptWidth);

  return (
    <Box flexDirection="column" flexShrink={0}>
      {/* Input area */}
      {showInput && (
        <Box
          flexDirection="column"
          paddingLeft={1} paddingRight={1}
          borderStyle="single"
          borderLeft={false}
          borderRight={false}
          borderColor={effectiveColor}
        >
          <Box flexDirection="row">
            {effectiveCommand && !explicitCommand ? null : (
              <Ansi>
                {explicitCommand
                  ? colors.input.command.prompt
                  : colors.input.normal.prompt}
              </Ansi>
            )}
            {projectListIdle ? (
              <Text dimColor>{t({en:"Type / for commands",zh:"输入 / 打开指令"})}</Text>
            ) : isOverridden || effectiveCommand ? (
              <TextInput
                value={isOverridden && footerOverride.secret ? "•".repeat(activeCmdInput.value.length) : activeCmdInput.value}
                cursorOffset={activeCmdInput.cursor}
                showCursor={editorFocused&&!projectListIdle&&!(isOverridden&&footerOverride.readOnly)}
                color={effectiveCommand ? colors.input.command.fg : undefined}
                maxWidth={inputMaxWidth}
              />
            ) : (
              <TextInput
                value={textInput.value}
                cursorOffset={textInput.cursor}
                showCursor={editorFocused}
                maxWidth={inputMaxWidth}
              />
            )}
          </Box>
        </Box>
      )}

      {/* Command result — below the input box */}
      {showInput && !footerDrawer && activeCmdResult ? (
        <Box flexDirection="row" paddingLeft={2} marginTop={1}>
          <Text color={colors.input.result.fg}>{activeCmdResult}</Text>
        </Box>
      ) : null}

      {/* Suggestions — vertical list outside the box, pushes input up */}
      {footerDrawer ? <Box flexDirection="column" paddingLeft={geometry.choice.inset} marginTop={1}>{footerDrawer}</Box> : showSuggestions && (() => {
        const maxVisible = geometry.choice.maxVisible;
        const idx = activeSuggestionIdx % activeSuggestions.length;
        const half = Math.floor(maxVisible / 2);
        const windowStart = Math.max(0, Math.min(idx - half, Math.max(0, activeSuggestions.length - maxVisible)));
        const visibleSlice = activeSuggestions.slice(windowStart, windowStart + maxVisible);
        return (
        <Box flexDirection="column" paddingLeft={geometry.choice.inset} marginTop={1}>
          {visibleSlice.map((s, i) => {
            const actualIdx = windowStart + i;
            const active = actualIdx === idx;
            return (
              <SuggestionRow key={s.label} selected={active} label={s.label} description={s.description}/>
            );
          })}
        </Box>
        );
      })()}

      {/* Status text */}
      <Box paddingLeft={1} paddingRight={1} marginTop={showInput ? 1 : 0}>
        {sceneStatus && (runningB !== undefined || isChatScene(scene)) ? (
          <RunningBStrip statusText={activeStatusText} language={language} width={Math.max(1, width - 2)}
            advancement={isChatScene(scene) ? advancement ?? null : undefined} advancementPending={advancementPending}
            runningB={runningB ?? []} selIdx={runningBSelIdx ?? 0} focused={statusBarFocused ?? false} contextPct={contextPct ?? ""} cachePct={cachePct} />
        ) : (
          <>
            {contextValue ? <><ContextStatus contextPct={contextValue} cachePct={cachePct} /><Text>  </Text></> : null}
            <Text dimColor>{activeStatusText || " "}</Text>
          </>
        )}
      </Box>
    </Box>
  );
});
