import React, { useEffect, useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { colors, cacheHitColor } from "../theme/tokens.js";
import { useAnimatedColor } from "../theme/useColorTransition.js";

/** Shared by A and B; one palette and one animation implementation. */
export function ContextStatus({ contextPct = "0%", cachePct }: {
  contextPct?: string; cachePct?: number | null;
}) {
  const value = Number.parseFloat(contextPct);
  const contextColor = useAnimatedColor(Number.isFinite(value) && value > 85
    ? colors.contextUsage.high : colors.contextUsage.normal);
  const cacheColor = useAnimatedColor(cacheHitColor(cachePct));
  const [showCache, setShowCache] = useState(false);
  const showTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clearTimers = () => {
    if (showTimer.current) clearTimeout(showTimer.current);
    if (hideTimer.current) clearTimeout(hideTimer.current);
    showTimer.current = null;
    hideTimer.current = null;
  };
  useEffect(() => clearTimers, []);
  const onEnter = () => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = null;
    if (showCache || showTimer.current) return;
    showTimer.current = setTimeout(() => {
      showTimer.current = null;
      setShowCache(true);
    }, 500);
  };
  const onLeave = () => {
    if (showTimer.current) clearTimeout(showTimer.current);
    showTimer.current = null;
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => {
      hideTimer.current = null;
      setShowCache(false);
    }, 1000);
  };
  const exact = cachePct == null || !Number.isFinite(cachePct)
    ? "Cache unavailable"
    : `Cache ${Number(cachePct.toFixed(1))}%`;
  return <Box flexDirection="row" position="relative" onMouseEnter={onEnter} onMouseLeave={onLeave}>
    <Text color={cacheColor}>●</Text>
    <Text color={contextColor}> {contextPct}</Text>
    {showCache ? <Box position="absolute" bottom={1} left={0}
      borderStyle="single" borderColor={colors.named.gray} paddingLeft={1} paddingRight={1}>
      <Text>{exact}</Text>
    </Box> : null}
  </Box>;
}
