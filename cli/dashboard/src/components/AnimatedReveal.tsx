import React, { useLayoutEffect, useRef, useState } from "react";
import { Box, measureElement, type DOMElement } from "@anthropic/ink";
import { colors } from "../theme/index.js";
import { animateNumber } from "../theme/motion.js";
import {useContentViewport} from '../ui/ContentViewport.js';

/** Animate explicit toggles. Open lazy/virtual content grows naturally, without
 * restarting a clipping animation on every newly measured body page. */
export function AnimatedReveal({
  expanded,
  children,
}: {
  expanded: boolean;
  children: React.ReactNode;
}) {
  const viewport=useContentViewport();
  // Virtual readers restore semantic anchors when geometry changes. Height
  // animation would repeatedly move the hit target and remeasure the virtual
  // tree for one click. Commit reader geometry once; other scenes keep motion.
  return viewport?<Box flexDirection='column' flexShrink={0}>{expanded?children:null}</Box>
    :<MotionReveal expanded={expanded}>{children}</MotionReveal>;
}
function MotionReveal({expanded,children}:{expanded:boolean;children:React.ReactNode}){
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
    if (inner.current) {
      const next = Math.max(0, Math.ceil(measureElement(inner.current).height));
      measured.current = next;
    }
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
