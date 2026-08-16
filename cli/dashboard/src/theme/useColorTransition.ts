// src/theme/useColorTransition.ts — Color lerp animation hook.

import { useState, useEffect } from "react";
import { useInterval } from "usehooks-ts";
import { lerpColor } from "./typography.js";
import { colors } from "./tokens.js";

export function useColorTransition(
  condition: boolean,
  fromColor: string,
  toColor: string,
  opts?: { frameCount?: number; intervalMs?: number },
): string {
  const frameCount = opts?.frameCount ?? colors.animation.lerp.frames;
  const intervalMs = opts?.intervalMs ?? colors.animation.lerp.intervalMs;

  const [frame, setFrame] = useState(0);
  const [animating, setAnimating] = useState(false);

  useEffect(() => {
    setFrame(0);
    setAnimating(true);
  }, [condition]);

  useInterval(
    () => {
      if (!animating) return;
      setFrame((f) => {
        if (f + 1 >= frameCount) setAnimating(false);
        return f + 1;
      });
    },
    animating ? intervalMs : null,
  );

  const targetColor = condition ? toColor : fromColor;
  if (!animating) return targetColor;

  const from = condition ? fromColor : toColor;
  const to = condition ? toColor : fromColor;
  return lerpColor(from, to, (frame + 1) / (frameCount + 1));
}
