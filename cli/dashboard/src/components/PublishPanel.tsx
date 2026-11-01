import React, {useEffect, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {publishGet, publishSet} from "../backend/tools.js";
import type {PublishLocalState} from "../state/publishLocalState.js";
import type {UseTextInputReturn} from "../hooks/useTextInput.js";
import {usePublishForm} from "../hooks/usePublishForm.js";
import {usePublishFormEditor} from "../hooks/usePublishFormEditor.js";
import {useManagedInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import {colors, usePanelSize} from "../theme/index.js";
import {HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";
import type {FooterConfig} from "./CommandBar.js";
import {formatChecks, parseChecks} from "./publishChecks.js";
import type {PublishCheck} from "../backend/publish.js";
import {publishCommand} from "../backend/publish.js";
import {randomUUID} from "node:crypto";
import {useFeedback} from "../hooks/useFeedback.js";
import {FeedbackMessage} from "./FeedbackMessage.js";

type Props = {
  localState?: PublishLocalState; client: BackendClient; project: string; cmdInput: UseTextInputReturn;
  onFooter: (config: FooterConfig | null) => void; onDismiss: () => void;
  onOpenTrial: () => void; onOpenFormal: () => void; onOpenAccount?: () => void;
};
type Data = {commit_format: Record<string, unknown>; privacy: Record<string, unknown>; authorship?: Record<string, unknown>;
  remote_url: string; repositories?: Record<string, string>; checks?: PublishCheck[]};
const tabs: Copy[] = [{en: "Repository / branches", zh: "仓库与分支"}, {en: "Account", zh: "账户"},
  {en: "Commit conventions", zh: "提交规范"}, {en: "Privacy / authorship", zh: "隐私与署名"}, {en: "Verification", zh: "验证检查"}];
const labels: Record<string, Copy> = {
  remote_url: {en: "Remote repository", zh: "远端仓库"}, trial: {en: "Inspect Trial changes", zh: "查看试用变更"}, formal: {en: "Inspect Formal commits", zh: "查看正式提交"},
  account: {en: "Configure repository account", zh: "配置仓库账户"}, prefix: {en: "Prefix", zh: "前缀"}, number_start: {en: "Starting number", zh: "起始编号"},
  padding: {en: "Zero padding", zh: "编号补零"}, plugins: {en: "Commit plugins (; separated)", zh: "提交插件（; 分隔）"}, template_name: {en: "Template", zh: "模板"},
  enabled: {en: "Privacy scan", zh: "隐私扫描"}, deep_scan: {en: "Deep scan", zh: "深度扫描"}, content_level: {en: "Content level", zh: "内容级别"},
  severity_threshold: {en: "Minimum severity", zh: "最低严重级别"}, exclude_paths: {en: "Excluded paths (; separated)", zh: "排除路径（; 分隔）"},
  include_paths: {en: "Explicit includes (; separated)", zh: "显式包含（; 分隔）"}, ignored_rules: {en: "Ignored rules (; separated)", zh: "忽略规则（; 分隔）"},
  approved_fingerprints: {en: "Approved fingerprints (; separated)", zh: "已批准匹配（; 分隔）"}, extra_patterns: {en: "Custom secret rules (JSON array)", zh: "自定义秘密规则（JSON 数组）"},
  mode: {en: "Authorship mode", zh: "署名策略"}, strip_commit_coauthors: {en: "Remove tool coauthor trailers", zh: "清理工具共同署名"},
  strip_code_comments: {en: "Clean tool authorship comments", zh: "清理工具署名注释"}, checks: {en: "Required checks", zh: "必需检查"},
};
const privacyFields = ["enabled", "deep_scan", "content_level", "severity_threshold", "exclude_paths", "include_paths", "ignored_rules", "approved_fingerprints", "extra_patterns"];
const authorshipFields = ["mode", "strip_commit_coauthors", "strip_code_comments"];
const arrays = ["plugins", "exclude_paths", "include_paths", "ignored_rules", "approved_fingerprints"];
const fieldSets = [["remote_url", "trial", "formal"], ["account"], ["prefix", "number_start", "padding", "plugins", "template_name"], [...privacyFields, ...authorshipFields], ["checks"]];

export function PublishPanel(props: Props) {
  const t = useCopy(), {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [tab, setTab] = useState(() => Math.max(0, Math.min(4, props.localState?.get<number>("settings.tab") ?? 0)));
  const [focus, setFocus] = useState<"header" | "body">("header"), [data, setData] = useState<Data | null>(null);
  const feedback = useFeedback({kind: "page", id: `${props.project}:publish/settings/${tab}`}, {tone: "error"});
  const error = feedback.text, setError = feedback.show;
  const read = () => {void publishGet(props.client, props.project).then(v => setData(v as Data)).catch(e => setError(String(e?.message || e)));};
  useEffect(read, [props.client, props.project]);
  useEffect(() => {props.localState?.set("settings.tab", tab);}, [tab]);
  useEffect(() => {if (focus === "header") {props.onFooter({hidden: true}); return () => props.onFooter(null);}}, [focus, props.onFooter]);
  useManagedInput((text, key) => {
    if (matchChord("escape", text, key)) props.onDismiss();
    else if (matchChord("left", text, key)) setTab(v => Math.max(0, v - 1));
    else if (matchChord("right", text, key)) setTab(v => Math.min(tabs.length - 1, v + 1));
    else if (matchChord("enter", text, key)) setFocus("body");
  }, {isActive: focus === "header"});
  return <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
    <HorizontalHeaderStrip items={tabs.map((label, index) => ({id: String(index), label: t(label)}))} selected={tab} width={w} contentFocused={focus !== "header"}/>
    <FeedbackMessage feedback={feedback.message}/>
    {!data ? <Text dimColor>{t({en: "Reading settings…", zh: "正在读取配置…"})}</Text> : tab === 1 ?
      <AccountFields {...props} focused={focus === "body"} onDismiss={() => setFocus("header")}/> :
      <SettingFields key={tab} {...props} data={data} tab={tab} focused={focus === "body"} onDismiss={() => setFocus("header")} onSaved={value => setData(value)}/>}
    {focus === "header" ? <Text dimColor>{t({en: "←/→ section · Enter select fields · Esc back", zh: "←/→ 分区 · Enter 选择字段 · Esc 返回"})}</Text> : null}
  </Box>;
}

/** Account fields belong directly to the Account section, not a third menu. */
function AccountFields(props: Props & {focused: boolean}) {
  const t = useCopy(), [busy, setBusy] = useState(false);
  const feedback = useFeedback({kind: "page", id: `${props.project}:publish/account`});
  const status = feedback.text, setStatus = feedback.show;
  const defaults = {kind: "github", instance: "https://github.com", repository: "", account: "", token: ""};
  const form = usePublishForm(props.localState, "settings/account", defaults, props.project, () => Object.keys(defaults));
  const accountLabels: Record<string, Copy> = {kind: {en: "Platform", zh: "平台"}, instance: {en: "Instance", zh: "实例地址"},
    repository: {en: "Repository", zh: "仓库"}, account: {en: "Account", zh: "账户"}, token: {en: "Access token", zh: "访问令牌"}};
  const editor = usePublishFormEditor(form, {busy, isActive: props.focused, onFooter: props.onFooter, onDismiss: props.onDismiss,
    choices: {kind: ["github", "gitlab"]}, onSave: async values => {
      setBusy(true); setStatus("");
      try {
        const {token, ...target} = values;
        await publishCommand(props.client, props.project, "account.save", {target, token}, randomUUID());
        form.setValues({...values, token: ""}); if (form.field === "token") form.input.setValue("");
        setStatus(t({en: "Credential saved. Repository access still verifies this account.", zh: "凭据已保存；读取仓库时仍会核对账户。"}));
      } catch (error: any) {setStatus(String(error?.message || error), {tone: "error"});}
      finally {setBusy(false);}
    }});
  return <Box flexDirection="column" flexGrow={1} minHeight={0}>
    {editor.rows(name => t(accountLabels[name]!))}
    <FeedbackMessage feedback={feedback.message}/>
  </Box>;
}

function SettingFields(props: Props & {data: Data; tab: number; focused: boolean; onSaved: (data: Data) => void}) {
  const {data, tab} = props, t = useCopy(), {h} = usePanelSize({minWidth: 20});
  const defaults: Record<string, string> = {};
  const source = tab === 0 ? {remote_url: data.remote_url, trial: "↵", formal: "↵"} : tab === 1 ? {account: "↵"} : tab === 2 ? data.commit_format
    : tab === 3 ? {...data.privacy, mode: "mixed", strip_commit_coauthors: true, strip_code_comments: false, ...data.authorship} : {checks: formatChecks(data.checks ?? [])};
  for (const name of fieldSets[tab]!) {
    const value = (source as Record<string, unknown>)[name];
    defaults[name] = name === "extra_patterns" ? JSON.stringify(value ?? []) : Array.isArray(value) ? value.join("; ") : String(value ?? "");
  }
  const form = usePublishForm(props.localState, `settings/group/${tab}`, defaults, JSON.stringify(data), () => fieldSets[tab]!, {persistPristine: false});
  const [busy, setBusy] = useState(false);
  const feedback = useFeedback({kind: "page", id: `${props.project}:publish/settings/group/${tab}`}, {tone: "error"});
  const status = feedback.text, setStatus = feedback.show;
  const editor = usePublishFormEditor(form, {busy, isActive: props.focused, onFooter: props.onFooter, onDismiss: props.onDismiss,
    choices: {padding: ["false", "true"], enabled: ["true", "false"], deep_scan: ["false", "true"], content_level: ["1", "2", "3"],
      severity_threshold: ["low", "medium", "high", "critical"], mode: ["mixed", "clean"], strip_commit_coauthors: ["true", "false"], strip_code_comments: ["false", "true"]},
    actions: {trial: props.onOpenTrial, formal: props.onOpenFormal, account: props.onOpenAccount ?? (() => setStatus(t({en: "Open account settings from Publish.", zh: "请从 Publish 打开账户配置。"})))},
    onSave: async values => {
      if (busy || tab === 1) return;
      setBusy(true); setStatus("");
      try {
        const parsed: Record<string, unknown> = {};
        for (const name of fieldSets[tab]!.filter(name => !["trial", "formal"].includes(name))) {
          const raw = values[name]!.trim();
          parsed[name] = arrays.includes(name) ? raw.split(";").map(v => v.trim()).filter(Boolean)
            : name === "extra_patterns" ? JSON.parse(raw) : name === "number_start" || name === "content_level" ? Number(raw)
            : ["enabled", "deep_scan", "padding", "strip_commit_coauthors", "strip_code_comments"].includes(name) ? raw === "true" : raw;
        }
        if (tab === 2 && (!Number.isInteger(parsed.number_start) || Number(parsed.number_start) < 0)) throw Error(t({en: "Starting number must be a non-negative integer", zh: "起始编号必须为非负整数"}));
        const section = ["remote_url", "", "commit_format", "privacy_authorship", "checks"][tab]!;
        const value = tab === 0 ? parsed.remote_url : tab === 2 ? {...data.commit_format, ...parsed} : tab === 4 ? parseChecks(values.checks!, data.checks)
          : {privacy: {...data.privacy, ...Object.fromEntries(privacyFields.map(name => [name, parsed[name]]))}, authorship: {...data.authorship, ...Object.fromEntries(authorshipFields.map(name => [name, parsed[name]]))}};
        const expected = tab === 0 ? data.remote_url : tab === 2 ? data.commit_format : tab === 4 ? data.checks ?? []
          : {privacy: data.privacy, authorship: data.authorship ?? {}};
        await publishSet(props.client, props.project, section, value, expected);
        props.localState?.remove(`settings/group/${tab}`);
        const saved = await publishGet(props.client, props.project) as Data;
        form.accept(values, JSON.stringify(saved));
        props.onSaved(saved); props.onDismiss();
      } catch (e: any) {
        setStatus(String(e?.message || e));
        try {props.onSaved(await publishGet(props.client, props.project) as Data);} catch { /* Preserve the draft and original failure. */ }
      } finally {setBusy(false);}
    }});
  const count = Math.max(1, h - 13), start = Math.max(0, Math.min(form.index - Math.floor(count / 2), form.fields.length - count));
  return <Box flexDirection="column" flexGrow={1} minHeight={0}>
    {tab === 0 ? Object.entries(data.repositories ?? {}).map(([role, path]) => <Text key={role} dimColor wrap="truncate-end">{role}: {path || "—"}</Text>) : null}
    {tab === 3 ? <Text dimColor>{t({en: "Privacy and authorship save together. Changes invalidate earlier prepared plans.", zh: "隐私与署名一起保存；修改后已有预备计划需要重新检查。"})}</Text> : null}
    {tab === 4 ? <Text dimColor>{t({en: "file.py | seeds | seconds; other.py | 42 | 30. Every configured check is required.", zh: "file.py | 随机种子 | 秒数; other.py | 42 | 30。所有已配置检查都必须通过。"})}</Text> : null}
    {editor.rows(name => t(labels[name]!), start, count)}
    {form.stale ? <Text>{t({en: "Settings changed. Ctrl+R reloads the current values.", zh: "配置已变化，请 Ctrl+R 重新读取当前值。"})}</Text> : null}
    <FeedbackMessage feedback={feedback.message}/>
  </Box>;
}
