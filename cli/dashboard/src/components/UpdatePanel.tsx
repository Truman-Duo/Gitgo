import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { updateStatus, type UpdateStatus } from "../backend/update.js";
import { colors } from "../theme/index.js";

export function UpdatePanel({client, cols, onDismiss}: {
  client: BackendClient; cols: number; onDismiss: () => void;
}) {
  const [data, setData] = useState<UpdateStatus | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [retry, setRetry] = useState(0);
  useInput((input, key) => {
    if (key.escape) onDismiss();
    else if (!key.paste && !key.ctrl && !key.meta && input.toLowerCase() === "r" && state !== "loading") setRetry(v => v + 1);
  }, {clipboard: false});
  useEffect(() => {
    let alive = true;
    setData(null); setState("loading");
    updateStatus(client).then(value => {
      if (alive) { setData(value); setState("ready"); }
    }).catch(() => { if (alive) setState("error"); });
    return () => { alive = false; };
  }, [client, retry]);
  return <Box flexDirection="column" paddingX={1} width={cols}>
    <Text bold>Gitgo Software Update</Text>
    <Text dimColor>Esc close · R refresh local status</Text>
    {state === "loading" ? <Text>Reading local update status…</Text> : null}
    {state === "error" ? <Text color={colors.danger}>Update status unavailable. Nothing was downloaded or changed. R retry.</Text> : null}
    {data ? <>
      <Text>Current version   {data.version}</Text>
      <Text>Channel           {data.channel}</Text>
      <Text>Software updates  Disabled</Text>
      <Text>Automatic checks  OFF</Text>
      <Text color={colors.warning}>This build cannot install or roll back software updates.</Text>
      {data.blockers.includes("MANAGED_INSTALLATION_REQUIRED")
        ? <Text>A managed installation is required; existing installs are not adopted.</Text>
        : <Text>Managed installation registered; software updates remain disabled.</Text>}
      <Text>Signed releases, update handoff and platform validation are required before enabling updates.</Text>
      <Text dimColor>This is Gitgo software status, not a project or model update. Refresh makes no release-network request.</Text>
    </> : null}
  </Box>;
}
