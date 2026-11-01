import {keyHintText} from "../input/hints.js";
import {PageFrame} from "../ui/PageFrame.js";
import {pages,reconcileSelection,standardKey} from "../ui/pageContract.js";
import {useListNavigation} from "../hooks/useListNavigation.js";
import {scrollViewport} from "../input/scrollViewport.js";
import React, {useEffect, useLayoutEffect, useRef, useState} from "react";
import {randomUUID} from "node:crypto";
import {Box, ScrollBox, Text, type DOMElement, type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput, useScrollInput} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {configureUpdate, updateStatus, checkUpdate, downloadUpdate, requestApplyUpdate, requestRollbackUpdate, cancelUpdate, cancelPendingUpdate, type UpdateStatus} from "../backend/update.js";
import {colors, usePanelSize} from "../theme/index.js";
import {FormFieldRow, fieldLabelWidth} from "./FormFieldRow.js";
import {SelectionRow, SelectionText} from "./Selection.js";

import {updateChinese} from "./updateText.js";

export function UpdatePanel({client, cols, onDismiss, onUpdateExit, language = "en"}: {
  language?: "en" | "zh";
  client: BackendClient; cols: number; onDismiss: () => void; onUpdateExit?: () => Promise<void>;
}) {
  const {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const t = (text: keyof typeof updateChinese) => keyHintText(language === "zh" ? updateChinese[text] : text);
  const [data, setData] = useState<UpdateStatus | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error" | "checking" | "downloading" | "requesting" | "cancelling" | "confirm" | "confirmApply" | "confirmRollback" | "confirmCancelPending" | "configuring">("loading");
  const [channel, setChannel] = useState<"stable" | "preview">("stable");
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState(0);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | boolean>(false);
  const scroll = useRef<ScrollBoxHandle>(null);
  const selectedRow = useRef<DOMElement>(null);
  const displayedStatus = useRef<UpdateStatus | null>(null);
  useScrollInput(scroll);
  useLayoutEffect(() => {
    // Read-only/source installations have one refresh action. Start at the
    // explanation rather than jumping to the last line on first entry.
    const navigable=updateSelectableRows(data,Boolean(onUpdateExit)).filter(row=>row.enabled).length > 1;
    if (!navigable && data && displayedStatus.current !== data) scroll.current?.scrollTo(0);
    displayedStatus.current=data;
    if (selectedRow.current && navigable)
      scroll.current?.scrollIntoView(selectedRow.current);
  }, [selected, data, state, w, h]);
  const [initialized, setInitialized] = useState(false);
  const alive = useRef(true);
  const job = useRef<{id: string; cancelled: boolean} | null>(null);
  const busy = ["checking", "downloading", "requesting", "cancelling", "configuring"].includes(state);
  const cancel = () => {
    if (!job.current) return;
    job.current.cancelled = true;
    setState("cancelling");
    void cancelUpdate(client, job.current.id).catch(() => {});
  };
  const run = (kind: "check" | "download" | "apply" | "rollback" | "cancelPending") => {
    if (job.current || !data || data.schema_version === 1) return;
    if (data.pending_restart && kind !== "cancelPending") return;
    if (kind === "cancelPending" && !data.pending_restart) return;
    if (kind === "check" && !data.can_check) return;
    if (kind === "download" && (!data.can_download || !data.candidate)) return;
    if (kind === "apply" && (!data.can_request_apply || !data.candidate || !onUpdateExit)) return;
    if (kind === "rollback" && (!data.can_request_rollback || !data.rollback || !onUpdateExit)) return;
    const current = {id: randomUUID().replaceAll("-", ""), cancelled: false};
    job.current = current;
    setState(kind === "check" ? "checking" : kind === "download" ? "downloading" : "requesting");
    const expectedStrategy = data.schema_version >= 5 ? data.apply_strategy : undefined;
    const promise = kind === "cancelPending"
      ? cancelPendingUpdate(client, data.pending_restart!.request_id, current.id).then(() => updateStatus(client, channel))
      : kind === "apply" || kind === "rollback" ? (kind === "apply"
      ? requestApplyUpdate(client, channel, data.candidate!.candidate_id, current.id, expectedStrategy)
      : requestRollbackUpdate(client, data.rollback!.rollback_id, current.id, expectedStrategy)).then(async () => {
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
  const changeSetting = (row: number, value: string | boolean) => {
    if (!data || data.schema_version === 1 || busy || job.current) return;
    if (data.schema_version !== 6 || !data.preferences) {
      if (row === 0) {
        setData(null); setState("loading");
        setChannel(value as "stable" | "preview");
        setRetry(value => value + 1);
      }
      return;
    }
    const preferences = {...data.preferences};
    if (row === 0) preferences.channel = value as "stable" | "preview";
    else {
      const field = (["automatic_check", "automatic_download", "automatic_install"] as const)[row - 1];
      if (!field) return;
      preferences[field] = Boolean(value);
    }
    const current = {id: randomUUID().replaceAll("-", ""), cancelled: false};
    job.current = current; setState("configuring");
    void configureUpdate(client, preferences).then(value => {
      if (alive.current && job.current === current && value.schema_version !== 1) {
        setChannel(value.channel); setData(value); setState("ready");
      }
    }).catch(() => {if (alive.current && job.current === current) setState("error");})
      .finally(() => {if (job.current === current) job.current = null;});
  };
  const availableRows = updateSelectableRows(data, Boolean(onUpdateExit));
  const navigateRows=useListNavigation(availableRows,selected,setSelected,scroll);
  const active = reconcileSelection(availableRows, selected);
  useLayoutEffect(() => { if (state !== "loading" && active !== undefined && active !== selected) setSelected(active); }, [active, selected, state]);
  const selectAction = (row: number) => {
    if (!availableRows.find(item => item.id === row)?.enabled) return;
    if (row <= 3 && data && data.schema_version !== 1) {
      setEditing(row);
      setDraft(row === 0 ? channel : Boolean(data.preferences?.[(["automatic_check", "automatic_download", "automatic_install"] as const)[row - 1]!]));
      return;
    }
    if (row === 4) run("check");
    else if (row === 5 && data && data.schema_version !== 1 && data.can_download) setState("confirm");
    else if (row === 6 && data && data.schema_version !== 1 && data.can_request_apply && onUpdateExit) setState("confirmApply");
    else if (row === 7 && data && data.schema_version !== 1 && data.can_request_rollback && onUpdateExit) setState("confirmRollback");
    else if (row === 8 && data && data.schema_version !== 1 && data.pending_restart) setState("confirmCancelPending");
    else if (row === 9) setRetry(value => value + 1);
  };
  useInput((input, key) => {
    const action=standardKey(input,key);
    if (editing !== null && !busy) {
      if (action === "back") {setEditing(null); return;}
      if (action === "left" || action === "right") setDraft(value => editing === 0 ? value === "stable" ? "preview" : "stable" : !value);
      if (action === "activate") {changeSetting(editing, draft); setEditing(null);}
      return;
    }
    if (action === "back") {
      if (["confirm","confirmApply","confirmRollback","confirmCancelPending"].includes(state)) {setState("ready"); return;}
      if (busy) cancel();
      onDismiss(); return;
    }
    if (busy || state === "loading") {
      if(action === "up" || action === "down")scrollViewport(scroll.current,action === "up" ? -1 : 1);
      return;
    }
    if (["confirm","confirmApply","confirmRollback","confirmCancelPending"].includes(state)) {
      if (action === "activate") run(state === "confirm" ? "download" : state === "confirmApply" ? "apply" : state === "confirmRollback" ? "rollback" : "cancelPending");
      return;
    }
    if (action === "up" || action === "down") {
      navigateRows(action === "up" ? -1 : 1); return;
    }
    if (action === "left" || action === "right") return;
    if (action === "activate" && active !== undefined) selectAction(active);
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
    updateStatus(client, initialized ? channel : undefined).then(value => {
      if (current && alive.current) {
        if (!initialized && value.schema_version !== 1) setChannel(value.channel);
        setInitialized(true); setData(value); setState("ready");
      }
    }).catch(() => {if (current && alive.current) setState("error");});
    return () => {current = false;};
  // Initial status/configuration acknowledgements already carry the selected
  // channel's full status. Only an explicit refresh/legacy view change reloads.
  }, [client, retry]);
  const row = (index: number, label: string, _available = true) => <SelectionRow key={index}
    selected={active === index} disabled={!availableRows[index]?.enabled} ref={active === index ? selectedRow : undefined} flexShrink={0}>
    <SelectionText>{label}</SelectionText>
  </SelectionRow>;
  const settingLabels = [t("Channel             ").trimEnd(), t("Automatic checks    ").trimEnd(), t("Automatic download  ").trimEnd(), t("Install on exit     ").trimEnd()];
  const labelWidth = fieldLabelWidth(settingLabels, Math.max(1, w - 12), "");
  const setting = (index: number, value: string | boolean) => <Box key={index} ref={active === index ? selectedRow : undefined}>
    <FormFieldRow label={settingLabels[index]!} selected={active === index} disabled={!availableRows[index]?.enabled}
      editing={editing === index} labelWidth={labelWidth} suffix="" highlight={false}>
      <SelectionText>{typeof (editing === index ? draft : value) === "boolean"
        ? (editing === index ? draft : value) ? t("ON") : t("OFF") : t((editing === index ? draft : value) as "stable" | "preview")}</SelectionText>
    </FormFieldRow>
  </Box>;
  return <PageFrame page={pages.update} width={Math.min(w, cols)}>
    <Text bold>{t("Gitgo Software Update")}</Text>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
    {state === "loading" ? <Text>{t("Reading local update status\u2026")}</Text> : null}
    {state === "checking" ? <Text>{t("Checking publisher-signed release metadata\u2026")}</Text> : null}
    {state === "configuring" ? <Text>{t("Saving update settings\u2026")}</Text> : null}
    {state === "downloading" ? <Text>{t("Downloading and verifying the selected full package\u2026")}</Text> : null}
    {state === "requesting" ? <Text>{t("Rechecking the confirmed program selection before safe shutdown\u2026")}</Text> : null}
    {state === "cancelling" ? <Text>{t("Cancelling; waiting for the bounded transfer to stop\u2026")}</Text> : null}
    {state === "error" ? <Text color={colors.danger}>{t("Update status unavailable or preparation stopped. No software was installed. Select Refresh to retry.")}</Text> : null}
    {data ? <>
      <Text>{t("Current version   ")}{data.version}</Text>
      {setting(0, data.schema_version !== 1 ? channel : data.channel)}
      {setting(1, Boolean(data.automatic_check))}
      {setting(2, data.schema_version !== 1 && Boolean(data.preferences?.automatic_download))}
      {setting(3, data.schema_version !== 1 && Boolean(data.preferences?.automatic_install))}
      {data.schema_version === 6 && !data.automatic_install_available ? <Text color={colors.warning}>{t("Automatic installation requires the current managed launcher. Checking/downloading remain separate capabilities.")}</Text> : null}
      {data.schema_version !== 1 && data.automatic ? <Text dimColor>{t("Background: ")}{Object.hasOwn(updateChinese,data.automatic.state) ? t(data.automatic.state as keyof typeof updateChinese) : data.automatic.state}{data.automatic.error_code ? " · " + data.automatic.error_code : ""}</Text> : null}
      {data.schema_version === 1 ? <>
        <Text>{t("Software updates  Disabled")}</Text>
        <Text color={colors.warning}>{t("This is not a managed installation. Update settings, downloads and installation are unavailable; only local status refresh is available.")}</Text>
        <Text>{t("A new managed installation and publisher public release trust are required.")}</Text>
      </> : <>
        <Text>{t("Latest version    ")}{data.latest_version || t("Not checked")}</Text>
        {data.apply_strategy === "after_reboot" ? <Text>{t("Replacement mode  After a manual macOS restart, not immediately")}</Text> : null}
        {data.pending_restart ? <Text color={colors.warning}>{t("Confirmed replacement awaits a manual macOS restart; select Cancel pending restart to review cancellation.")} Gitgo {data.pending_restart.version}</Text> : null}
        {state === "confirmCancelPending" ? <Text color={colors.warning}>{t("Cancel this pending replacement? The current program and prepared package remain unchanged.")}</Text> : null}
        {data.status === "unconfigured" ? <Text color={colors.warning}>{t("Publisher release source/public keys are not configured for this channel. No network request.")}</Text> : null}
        {data.status === "idle" ? <Text>{t("Check now to query the selected channel. Background checks follow the saved settings.")}</Text> : null}
        {data.status === "stale" ? <Text color={colors.warning}>{t("Cached release or download is stale/changed. Select Check now.")}</Text> : null}
        {data.status === "up_to_date" ? <Text>{t("No newer compatible release is available.")}</Text> : null}
        {data.candidate ? <Text>{t("Package           ")}{data.candidate.release_id} · {(data.candidate.bytes / 1024 / 1024).toFixed(1)} MiB</Text> : null}
        {data.status === "available" && state !== "confirm" ? <Text>{t("Signed release available. Select Download to review.")}</Text> : null}
        {state === "confirm" ? <Text color={colors.warning}>{t("Download this exact signed package? This only prepares files; it does not install or run them.")}</Text> : null}
        {data.status === "downloaded" ? <Text color={colors.success}>{t("Downloaded and verified. Current program is unchanged.")}</Text> : null}
        {data.status === "downloaded" && (!data.can_request_apply || !onUpdateExit) ? <Text color={colors.warning}>{t("Restart/update is unavailable here: use a supported managed launcher with complete process cleanup.")}</Text> : null}
        {data.can_request_apply && onUpdateExit && state !== "confirmApply" ? <Text>{t("Select Restart / update to review.")}</Text> : null}
        {state === "confirmApply" ? <Text color={colors.warning}>{data.apply_strategy === "after_reboot" ? t("Close this Gitgo session and save this exact update for after you manually restart macOS? No program is replaced now. On next boot, the fixed launcher reverifies signature/package and startup health; failures preserve or restore the old program.") : t("Close this Gitgo session and attempt the selected update? The stable launcher will reverify, switch and check health; failures restore the previous program. Other running instances block replacement.")}</Text> : null}
        {data.rollback ? <Text>{t("Retained version  ")}{data.rollback.version}</Text> : null}
        {data.blockers.includes("UPDATE_REVERSE_MIGRATION_REQUIRED") ? <Text color={colors.warning}>{t("This update migrated data. Restoring the previous program requires a compatible reverse migration; current user data will not be replaced by an old snapshot.")}</Text> : null}
        {data.can_request_rollback && onUpdateExit && state !== "confirmRollback" ? <Text>{t("Select Rollback to review the retained version.")}</Text> : null}
        {state === "confirmRollback" ? <Text color={colors.warning}>{t("Close this session and roll back? The retained program must pass startup checks; other instances block replacement. Projects, settings and databases are not rolled back. macOS requires a manual restart.")} Gitgo {data.rollback?.version}</Text> : null}
      </>}
      {row(4, t("Check now"), data.schema_version !== 1 && data.can_check)}
      {row(5, t("Download selected release"), data.schema_version !== 1 && data.can_download)}
      {row(6, t("Review restart / update"), data.schema_version !== 1 && data.can_request_apply && Boolean(onUpdateExit))}
      {row(7, t("Review rollback"), data.schema_version !== 1 && data.can_request_rollback && Boolean(onUpdateExit))}
      {row(8, t("Cancel pending restart"), data.schema_version !== 1 && Boolean(data.pending_restart))}
      {row(9, t("Refresh local status"))}
      <Text dimColor>{t("Gitgo software only. Automatic installation waits for a clean exit; active tasks and other instances are never forcibly stopped. Rollback requires confirmation.")}</Text>
    </> : null}
    {!data && state === "error" ? row(9, t("Refresh local status")) : null}
    </ScrollBox>
    <Text dimColor>{t(busy ? "Esc cancel and close" : editing !== null ? "←/→ choose · Enter save · Esc cancel" : state === "confirmCancelPending" ? "Enter cancel pending replacement · Esc keep it" : state === "confirmRollback" ? "Enter confirm rollback · Esc cancel" : state === "confirmApply" ? "Enter confirm restart/update · Esc cancel" : state === "confirm" ? "Enter confirm download · Esc cancel" : availableRows.filter(row=>row.enabled).length < 2 ? "Only Refresh is available · Enter refresh · Esc close · ↑/↓ browse overflow" : "↑/↓ select · Enter open · Esc close")}</Text>
  </PageFrame>;
}


export function updateSelectableRows(data: UpdateStatus | null, canExit: boolean) {
 const managed=Boolean(data && data.schema_version !== 1);
 const current=data && data.schema_version !== 1 ? data : null;
 const free=Boolean(current && !current.pending_restart);
 return [
  {id:0,enabled:managed && free},
  ...[1,2,3].map(id=>({id,enabled:data?.schema_version === 6 && free})),
  {id:4,enabled:free && Boolean(current?.can_check)},
  {id:5,enabled:free && Boolean(current?.can_download)},
  {id:6,enabled:free && Boolean(current?.can_request_apply) && canExit},
  {id:7,enabled:free && Boolean(current?.can_request_rollback) && canExit},
  {id:8,enabled:Boolean(current?.pending_restart)},
  {id:9,enabled:true},
 ];
}
