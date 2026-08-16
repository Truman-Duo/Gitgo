import React, { useState, useEffect } from "react";
import { Text } from "@anthropic/ink";
import { colors } from "../theme/index.js";

export function Spinner({ frames, intervalMs, color }: {
  frames?: readonly string[];
  intervalMs?: number;
  color?: string;
}) {
  const f = frames ?? colors.spinner.frames;
  const iv = intervalMs ?? colors.spinner.intervalMs;
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setFrame((x) => (x + 1) % f.length), iv);
    return () => clearInterval(id);
  }, [f, iv]);
  return <Text color={color}>{f[frame]}</Text>;
}
