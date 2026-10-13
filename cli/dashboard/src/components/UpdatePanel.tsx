import React, {useEffect, useRef, useState} from "react";
import {randomUUID} from "node:crypto";
import {Box, Text} from "@anthropic/ink";
import {useManagedInput as useInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {updateStatus, checkUpdate, downloadUpdate, cancelUpdate, type UpdateStatus} from "../backend/update.js";
import {colors} from "../theme/index.js";

export function UpdatePanel({client, cols, onDismiss}: {
  client: BackendClient; cols: number; onDismiss: () => void;
}) {
  const [data, setData] = useState<UpdateStatus | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error" | "checking" | "downloading" | "cancelling" | "confirm">("loading");
  const [channel, setChannel] = useState<"stable" | "preview">("stable");
  const [retry, setRetry] = useState(0);
  const alive = useRef(true);
  const job = useRef<{id: string} | null>(null);
  const busy = ["checking", "downloading", "cancelling"].includes(state);
  const cancel = () => {
    if (!job.current) return;
    setState("cancelling");
    void cancelUpdate(client, job.current.id).catch(() => {});
  };
  const run = (kind: "check" | "download") => {
    if (job.current || !data || data.schema_version !== 2) return;
    if (kind === "check" && !data.can_check) return;
    if (kind === "download" && (!data.can_download || !data.candidate)) return;
    const current = {id: randomUUID().replaceAll("-", "")};
    job.current = current;
    setState(kind === "check" ? "checking" : "downloading");
    const promise = kind === "check" ? checkUpdate(client, channel, current.id)
      : downloadUpdate(client, channel, data.candidate!.candidate_id, current.id);
    promise.then(value => {
      if (alive.current && job.current === current) {setData(value); setState("ready");}
    }).catch(() => {
      if (alive.current && job.current === current) {setData(null); setState("error");}
    }).finally(() => {if (job.current === current) job.current = null;});
  };
  useInput((input, key) => {
    if (key.escape) {
      if (state === "confirm") {setState("ready"); return;}
      if (busy) cancel();
      onDismiss(); return;
    }
    if (key.paste || key.ctrl || key.meta) return;
    if (busy) {if (input.toLowerCase() === "x") cancel(); return;}
    if (state === "confirm") {
      if (key.return) run("download");
      else if (input.toLowerCase() === "n") setState("ready");
      return;
    }
    if (state === "loading") return;
    const letter = input.toLowerCase();
    if (letter === "r") setRetry(value => value + 1);
    else if (letter === "c" && data?.schema_version === 2) setChannel(value => value === "stable" ? "preview" : "stable");
    else if (letter === "k") run("check");
    else if (letter === "d" && data?.schema_version === 2 && data.can_download) setState("confirm");
  }, {clipboard: false});
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (job.current) void cancelUpdate(client, job.current.id).catch(() => {});
      job.current = null;
    };
  }, [client]);
  useEffect(() => {
    let current = true;
    setData(null); setState("loading");
    updateStatus(client, channel).then(value => {
      if (current && alive.current) {setData(value); setState("ready");}
    }).catch(() => {if (current && alive.current) setState("error");});
    return () => {current = false;};
  }, [client, channel, retry]);
  return <Box flexDirection="column" paddingX={1} width={cols}>
    <Text bold>Gitgo Software Update</Text>
    <Text dimColor>{busy ? "X cancel · Esc cancel and close" : state === "confirm" ? "Enter confirm download · Esc/N cancel" : "Esc close · R local refresh · C channel · K check · D download"}</Text>
    {state === "loading" ? <Text>Reading local update status…</Text> : null}
    {state === "checking" ? <Text>Checking publisher-signed release metadata…</Text> : null}
    {state === "downloading" ? <Text>Downloading and verifying the selected full package…</Text> : null}
    {state === "cancelling" ? <Text>Cancelling; waiting for the bounded transfer to stop…</Text> : null}
    {state === "error" ? <Text color={colors.danger}>Update status unavailable or preparation stopped. No software was installed. R refresh and retry.</Text> : null}
    {data ? <>
      <Text>Current version   {data.version}</Text>
      <Text>Channel           {data.schema_version === 2 ? channel : data.channel}</Text>
      <Text>Automatic checks  OFF</Text>
      {data.schema_version === 1 ? <>
        <Text>Software updates  Disabled</Text>
        <Text>A new managed installation and publisher public release trust are required.</Text>
      </> : <>
        <Text>Latest version    {data.latest_version || "Not checked"}</Text>
        {data.status === "unconfigured" ? <Text color={colors.warning}>Publisher release source/public keys are not configured for this channel. No network request.</Text> : null}
        {data.status === "idle" ? <Text>K manually check for updates; opening this page never contacts the release server.</Text> : null}
        {data.status === "stale" ? <Text color={colors.warning}>Cached release or download is stale/changed. K check again.</Text> : null}
        {data.status === "up_to_date" ? <Text>No newer compatible release is available.</Text> : null}
        {data.candidate ? <Text>Package           {data.candidate.release_id} · {(data.candidate.bytes / 1024 / 1024).toFixed(1)} MiB</Text> : null}
        {data.status === "available" && state !== "confirm" ? <Text>Signed release available. D review and confirm download.</Text> : null}
        {state === "confirm" ? <Text color={colors.warning}>Download this exact signed package? This only prepares files; it does not install or run them.</Text> : null}
        {data.status === "downloaded" ? <Text color={colors.success}>Downloaded and verified. Current program is unchanged; installation is not available in this stage.</Text> : null}
      </>}
      <Text dimColor>Gitgo software only, not projects or models. This stage cannot install or roll back software.</Text>
    </> : null}
  </Box>;
}
