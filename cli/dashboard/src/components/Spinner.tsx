import React from "react";
import { Text } from "@anthropic/ink";
import type { Color } from "@anthropic/ink";
import { colors } from "../theme/index.js";
import {useSharedClock} from "../hooks/useSharedClock.js";

export function Spinner({ frames, intervalMs, color }: {
  frames?: readonly string[];
  intervalMs?: number;
  color?: Color;
}) {
  const f = frames ?? colors.spinner.frames;
  const iv = intervalMs ?? colors.spinner.intervalMs;
  const now = useSharedClock(iv);
  const frame = Math.floor(now / Math.max(16, iv)) % f.length;
  return <Text color={color}>{f[frame]}</Text>;
}
