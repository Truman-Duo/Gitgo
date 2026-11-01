import React, { useEffect, useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import type { PendingDecision } from "../types.js";
import { manualCompact } from "../backend/tools.js";
import { DecisionPanel } from "./DecisionPanel.js";
import { colors } from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";

/** Host maintenance choices use the existing decision UI, not another LLM turn. */
export function CompactPanel({ client, project, processId, result: initial, onDismiss }: {
  client: BackendClient; project: string; processId: string; result: any; onDismiss: () => void;
}) {
  const t=useCopy();
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
    <Text bold>{t({en:"Context compaction",zh:"上下文压缩"})}</Text>
    {busy ? <Text dimColor>{t({en:"Compacting…",zh:"正在压缩…"})} {decision ? t({en:"user-approved reset",zh:"用户已批准重置"}) : t({en:`attempt ${Number(result?.attempt || 0) + 1}/3`,zh:`第 ${Number(result?.attempt || 0) + 1}/3 次尝试`})} · {elapsed}s</Text> : null}
    {error ? <Text color={colors.danger}>{t({en:"[COMPACTION_REQUEST_FAILED] ",zh:"[COMPACTION_REQUEST_FAILED] "})}{error}</Text> : null}
    {decision ? <DecisionPanel decision={decision} selectedIndex={selection} composing={false} submitting={busy} /> : null}
    {!busy && !decision ? <Text color={result?.status === "failed" ? colors.danger : undefined}>
      {result?.status === "completed"
        ? result?.changed === false && result?.reason === "not_enough_foldable_history"
          ? t({en:"Context is already compact; there is not enough foldable history yet.",zh:"上下文已经精简，暂无足够历史可继续压缩。"})
          : t({en:"Context compacted",zh:"上下文已压缩"})
        : result?.status === "cancelled"
        ? t({en:"Compaction cancelled; conversation preserved.",zh:"压缩已取消，对话已保留。"})
        : `[${result?.error_info?.catalog_id || "CONTEXT_COMPACTION_FAILED"}] ${result?.error_info?.message || result?.error || t({en:"Compaction failed",zh:"压缩失败"})}`}
    </Text> : null}
    <Text dimColor>{busy ? t({en:"Waiting for the current compaction attempt.",zh:"正在等待本次压缩。"}) : t({en:"Esc close",zh:"Esc 关闭",keys:true})}</Text>
  </Box>;
}
