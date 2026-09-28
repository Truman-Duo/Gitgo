import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { Color } from "@anthropic/ink";
import type { RuntimeNotice, RuntimeNoticeSeverity } from "../types.js";
import { colors } from "../theme/index.js";

function severityColor(severity: RuntimeNoticeSeverity): Color {
  switch (severity) {
    case "success": return colors.success;
    case "warning": return colors.warning;
    case "error": return colors.danger;
    default: return colors.named.gray;
  }
}

export function RuntimeNoticeList({
  notices, verbose = true,
}: { notices: RuntimeNotice[]; verbose?: boolean }) {
  if (notices.length === 0) return null;
  // runtimeNotices already filters routine control-plane churn.  What remains
  // is actionable or semantically meaningful in both compact and verbose mode;
  // verbose only reveals its trace detail.
  const visible = notices;
  return (
    <Box flexDirection="column" paddingLeft={2} marginTop={1}>
      {verbose ? <Text dimColor>trajectory</Text> : null}
      {visible.map((notice) => <RuntimeNoticeRow key={notice.key} notice={notice} verbose={verbose}/>)}
    </Box>
  );
}

export function RuntimeNoticeRow({
  notice, verbose = true,
}: { notice: RuntimeNotice; verbose?: boolean }) {
  return <Box flexDirection="column">
    <Box flexDirection="row">
      <Text color={severityColor(notice.severity)}>◆ </Text>
      {notice.actor_label ? <Text dimColor>{notice.actor_label} · </Text> : null}
      <Text bold={notice.severity === "warning" || notice.severity === "error"}>
        {notice.label}
      </Text>
    </Box>
    {notice.detail || (verbose && notice.detail_ref) ? <Box paddingLeft={2}>
      <Text dimColor>
        {notice.detail || ""}{notice.detail && verbose && notice.detail_ref ? " · " : ""}
        {verbose && notice.detail_ref ? "trace ref available" : ""}
      </Text>
    </Box> : null}
  </Box>;
}
