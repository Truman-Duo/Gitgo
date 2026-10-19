import React, { useLayoutEffect, useRef, useState } from "react";
import { Box, measureElement, type DOMElement } from "@anthropic/ink";
import { colors } from "../theme/index.js";
import { animateNumber } from "../theme/motion.js";

/** Keep content mounted while closing, so virtualization observes bounded height changes. */
export function AnimatedReveal({
  expanded,
  children,
}: {
  expanded: boolean;
  children: React.ReactNode;
}) {
  const inner = useRef<DOMElement>(null);
  const measured = useRef(0);
  const initial = useRef(true);
  const current = useRef<number | undefined>(expanded ? undefined : 0);
  const [height, setHeight] = useState<number | undefined>(current.current);
  const update = (value: number | undefined) => {
    current.current = value;
    setHeight(value);
  };
  useLayoutEffect(() => {
    if (inner.current)
      measured.current = Math.max(
        0,
        Math.ceil(measureElement(inner.current).height),
      );
  });
  useLayoutEffect(() => {
    if (initial.current) {
      initial.current = false;
      return;
    }
    const target = expanded ? measured.current : 0;
    const from = current.current ?? measured.current;
    update(from);
    return animateNumber(
      from,
      target,
      (value) => {
        const done = Math.abs(value - target) < 0.01;
        update(
          done ? (expanded ? undefined : 0) : Math.max(0, Math.ceil(value)),
        );
      },
      {
        durationMs:
          process.env.GITGO_REDUCED_MOTION === "1"
            ? 0
            : colors.animation.collapse.durationMs,
        intervalMs: colors.animation.collapse.intervalMs,
      },
    );
  }, [expanded]);
  const present = expanded || height === undefined || height > 0;
  return (
    <Box
      flexDirection="column"
      height={height}
      overflow="hidden"
      flexShrink={0}
    >
      {present ? (
        <Box ref={inner} flexDirection="column" flexShrink={0}>
          {children}
        </Box>
      ) : null}
    </Box>
  );
}
