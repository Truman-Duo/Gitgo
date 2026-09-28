import React, { useEffect, useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import type { PendingDecision } from "../types.js";
import { manualCompact } from "../backend/tools.js";
import { DecisionPanel } from "./DecisionPanel.js";
import { colors } from "../theme/index.js";

/** Host maintenance choices use the existing decision UI, not another LLM turn. */
export function CompactPanel({ client, project, processId, result: initial, onDismiss }: {
  client: BackendClient; project: string; processId: string; result: any; onDismiss: () => void;
}) {
  const [result, setResult] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [selection, setSelection] = useState(0);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const inFlight = useRef(false);
  const started = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const run = async (decision?: { decision_id: string; choice: "force_compact" | "stop" }) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      const next = await manualCompact(client, project, processId, decision);
      if (alive.current) setResult(next);
    } catch (err) {
      if (alive.current) setError(String(err));
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  };
  useEffect(() => {
    if (result?.status !== "starting" || started.current) return;
    started.current = true;
    void run();
  }, []);
  useEffect(() => {
    if (result?.status === "failed" && result.retry_compaction && Number(result.attempt) < 3) {
      void run();
    }
  }, [result]);
  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const startedAt = Date.now();
    const timer = setInterval(() => setElapsed(Math.ceil((Date.now() - startedAt) / 1000)), 250);
    return () => clearInterval(timer);
  }, [busy]);

  const decision = result?.pending_decision as PendingDecision | undefined;
  useInput((_input, key) => {
    if (busy) return;
    if (key.escape) { onDismiss(); return; }
    if (!decision) return;
    if (key.leftArrow || key.upArrow || key.rightArrow || key.downArrow) {
      setSelection(index => (index + 1) % decision.options.length);
    }
    if (key.return) void run({ decision_id: decision.decision_id,
      choice: selection === 0 ? "force_compact" : "stop" });
  });

  return <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
    <Text bold>Context compaction</Text>
    {busy ? <Text dimColor>Compacting… {decision ? "user-approved reset" : `attempt ${Number(result?.attempt || 0) + 1}/3`} · {elapsed}s</Text> : null}
    {error ? <Text color={colors.danger}>[COMPACTION_REQUEST_FAILED] {error}</Text> : null}
    {decision ? <DecisionPanel decision={decision} selectedIndex={selection} composing={false} submitting={busy} /> : null}
    {!busy && !decision ? <Text color={result?.status === "failed" ? colors.danger : undefined}>
      {result?.status === "completed"
        ? result?.changed === false && result?.reason === "not_enough_foldable_history"
          ? "Context is already compact; there is not enough foldable history yet."
          : "Context compacted"
        : result?.status === "cancelled"
        ? "Compaction cancelled; conversation preserved."
        : `[${result?.error_info?.catalog_id || "CONTEXT_COMPACTION_FAILED"}] ${result?.error_info?.message || result?.error || "Compaction failed"}`}
    </Text> : null}
    <Text dimColor>{busy ? "Waiting for the current compaction attempt." : "Esc close"}</Text>
  </Box>;
}
