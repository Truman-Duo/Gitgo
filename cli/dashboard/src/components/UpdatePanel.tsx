import React, {useEffect, useRef, useState} from "react";
import {randomUUID} from "node:crypto";
import {Box, Text} from "@anthropic/ink";
import {useManagedInput as useInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {updateStatus, checkUpdate, downloadUpdate, requestApplyUpdate, requestRollbackUpdate, cancelUpdate, type UpdateStatus} from "../backend/update.js";
import {colors} from "../theme/index.js";

export function UpdatePanel({client, cols, onDismiss, onUpdateExit}: {
  client: BackendClient; cols: number; onDismiss: () => void; onUpdateExit?: () => Promise<void>;
}) {
  const [data, setData] = useState<UpdateStatus | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error" | "checking" | "downloading" | "requesting" | "cancelling" | "confirm" | "confirmApply" | "confirmRollback">("loading");
  const [channel, setChannel] = useState<"stable" | "preview">("stable");
  const [retry, setRetry] = useState(0);
  const alive = useRef(true);
  const job = useRef<{id: string; cancelled: boolean} | null>(null);
  const busy = ["checking", "downloading", "requesting", "cancelling"].includes(state);
  const cancel = () => {
    if (!job.current) return;
    job.current.cancelled = true;
    setState("cancelling");
    void cancelUpdate(client, job.current.id).catch(() => {});
  };
  const run = (kind: "check" | "download" | "apply" | "rollback") => {
    if (job.current || !data || data.schema_version === 1) return;
    if (kind === "check" && !data.can_check) return;
    if (kind === "download" && (!data.can_download || !data.candidate)) return;
    if (kind === "apply" && (!data.can_request_apply || !data.candidate || !onUpdateExit)) return;
    if (kind === "rollback" && (!data.can_request_rollback || !data.rollback || !onUpdateExit)) return;
    const current = {id: randomUUID().replaceAll("-", ""), cancelled: false};
    job.current = current;
    setState(kind === "check" ? "checking" : kind === "apply" || kind === "rollback" ? "requesting" : "downloading");
    const promise = kind === "apply" || kind === "rollback" ? (kind === "apply"
      ? requestApplyUpdate(client, channel, data.candidate!.candidate_id, current.id)
      : requestRollbackUpdate(client, data.rollback!.rollback_id, current.id)).then(async () => {
      // Confirmation does not install in this running process. Shutdown only
      // after the exact response; cancellation/closed UI must never exit 75.
      if (alive.current && job.current === current && !current.cancelled) await onUpdateExit!();
      return data;
    }) : kind === "check" ? checkUpdate(client, channel, current.id)
      : downloadUpdate(client, channel, data.candidate!.candidate_id, current.id);
    promise.then(value => {
      if (alive.current && job.current === current) {setData(value); setState("ready");}
    }).catch(() => {
      if (alive.current && job.current === current) {setData(null); setState("error");}
    }).finally(() => {if (job.current === current) job.current = null;});
  };
  useInput((input, key) => {
    if (key.escape) {
      if (state === "confirm" || state === "confirmApply" || state === "confirmRollback") {setState("ready"); return;}
      if (busy) cancel();
      onDismiss(); return;
    }
    if (key.paste || key.ctrl || key.meta) return;
    if (busy) {if (input.toLowerCase() === "x") cancel(); return;}
    if (state === "confirm" || state === "confirmApply" || state === "confirmRollback") {
      if (key.return) run(state === "confirm" ? "download" : state === "confirmApply" ? "apply" : "rollback");
      else if (input.toLowerCase() === "n") setState("ready");
      return;
    }
    if (state === "loading") return;
    const letter = input.toLowerCase();
    if (letter === "r") setRetry(value => value + 1);
    else if (letter === "c" && data && data.schema_version !== 1) setChannel(value => value === "stable" ? "preview" : "stable");
    else if (letter === "k") run("check");
    else if (letter === "d" && data && data.schema_version !== 1 && data.can_download) setState("confirm");
    else if (letter === "a" && data && data.schema_version !== 1 && data.can_request_apply && onUpdateExit) setState("confirmApply");
    else if (letter === "b" && data && data.schema_version === 4 && data.can_request_rollback && onUpdateExit) setState("confirmRollback");
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
    <Text dimColor>{busy ? "X cancel · Esc cancel and close" : state === "confirmRollback" ? "Enter confirm rollback · Esc/N cancel" : state === "confirmApply" ? "Enter confirm restart/update · Esc/N cancel" : state === "confirm" ? "Enter confirm download · Esc/N cancel" : "Esc close · R refresh · C channel · K check · D download · A apply · B rollback"}</Text>
    {state === "loading" ? <Text>Reading local update status…</Text> : null}
    {state === "checking" ? <Text>Checking publisher-signed release metadata…</Text> : null}
    {state === "downloading" ? <Text>Downloading and verifying the selected full package…</Text> : null}
    {state === "requesting" ? <Text>Rechecking the confirmed program selection before safe shutdown…</Text> : null}
    {state === "cancelling" ? <Text>Cancelling; waiting for the bounded transfer to stop…</Text> : null}
    {state === "error" ? <Text color={colors.danger}>Update status unavailable or preparation stopped. No software was installed. R refresh and retry.</Text> : null}
    {data ? <>
      <Text>Current version   {data.version}</Text>
      <Text>Channel           {data.schema_version !== 1 ? channel : data.channel}</Text>
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
        {data.status === "downloaded" ? <Text color={colors.success}>Downloaded and verified. Current program is unchanged.</Text> : null}
        {data.status === "downloaded" && (!data.can_request_apply || !onUpdateExit) ? <Text color={colors.warning}>Restart/update is unavailable here: use a supported managed launcher with complete process cleanup.</Text> : null}
        {data.can_request_apply && onUpdateExit && state !== "confirmApply" ? <Text>A review restart/update confirmation.</Text> : null}
        {state === "confirmApply" ? <Text color={colors.warning}>Close this Gitgo session and attempt the selected update? The stable launcher will reverify, switch and check health; failures restore the previous program. Other running instances block replacement.</Text> : null}
        {data.rollback ? <Text>Retained version  {data.rollback.version}</Text> : null}
        {data.can_request_rollback && onUpdateExit && state !== "confirmRollback" ? <Text>B review rollback to the retained version.</Text> : null}
        {state === "confirmRollback" ? <Text color={colors.warning}>Close this session and restore Gitgo {data.rollback?.version}? The retained program must pass startup checks. Projects, configuration and databases remain unchanged; other running instances block replacement.</Text> : null}
      </>}
      <Text dimColor>Gitgo software only, not projects or models. No automatic checks. Rollback requires explicit confirmation.</Text>
    </> : null}
  </Box>;
}
