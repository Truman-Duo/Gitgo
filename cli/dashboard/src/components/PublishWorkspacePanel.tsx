import React, {useCallback, useEffect, useLayoutEffect, useRef, useState} from "react";
import {randomUUID} from "node:crypto";
import {Box, ScrollBox, Text} from "@anthropic/ink";
import type {DOMElement, ScrollBoxHandle} from "@anthropic/ink";
import {publishCommand, publishItems, type PublishItem, type PublishSnapshot, type PublishSharedPage} from "../backend/publish.js";
import {matchChord} from "../input/bindings.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {applyTextOp, useTextInput} from "../hooks/useTextInput.js";
import {colors, separator, truncate, usePanelSize} from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";
import {HorizontalHeaderStrip} from "./HorizontalHeaderStrip.js";
import {PublishPanel} from "./PublishPanel.js";
import {PublishDraft} from "./PublishDraft.js";
import {PublishOperationDraft, type PublishOperationMode} from "./PublishOperationDraft.js";
import {PublishHistoryPanel} from "./PublishHistoryPanel.js";

type Props = React.ComponentProps<typeof PublishPanel>;
const NODE_NAMES: Record<string, Copy> = {
  record: {en: "Record", zh: "记录"}, verify: {en: "Verify", zh: "核对"},
  integrate: {en: "Integrate", zh: "合入"}, push: {en: "Push", zh: "推送"},
  collaborate: {en: "Discussion", zh: "协作"}, distribute: {en: "Version delivery", zh: "版本交付"}, branches: {en: "Branches", zh: "分支"},
};
const ACTION_COMMANDS: Record<string, string> = {share: "/share remote", share_publish: "/share-send", share_reconcile: "/share-reconcile",
  collaborate: "/remote · /comment · /review", collaborate_send: "/send draft-id", collaborate_reconcile: "/platform-reconcile draft-id",
  link_task: "/link", import_package: "/import", distribution_reconcile: "/distribution-reconcile", branch_apply: "/branch-apply", branch_reconcile: "/branch-reconcile"};
const REASONS: Record<string, Copy> = {
  PUBLISH_DISTRIBUTION_PENDING: {en: "Build or delivery has an unconfirmed result. /distribution-reconcile checks the saved operation.", zh: "构建或交付结果待确认，/distribution-reconcile 核对已保存操作。"},
  PUBLISH_BRANCH_RESULT_UNVERIFIED: {en: "Branch change is awaiting observation. /branch-reconcile does not repeat the write.", zh: "分支变更待核对；/branch-reconcile 不会重复写入。"},
  PUBLISH_DISTRIBUTION_RECORDED: {en: "The item records each version, delivery target and verification result separately.", zh: "事项分别保存版本、交付目标与核验结果。"},
  PUBLISH_COLLABORATION_RECORDED: {en: "Saved platform observations and drafts are available. Sending requires an explicit action.", zh: "可查看已保存的平台观察与草稿；发送需明确操作。"},
  PUBLISH_BRANCH_RECORDED: {en: "Retention and lifecycle results are recorded separately from integration.", zh: "分支保留与生命周期结果单独记录。"},
  PUBLISH_LEGACY_HISTORY_OBSERVED: {en: "The historical record is backed up. Its candidate matches the target content; prior checks and remote publication remain unverified.", zh: "历史记录已备份。候选与目标内容相同；旧检查及远端发布结果仍未核实。"},
  PUBLISH_SHARED_RESULT_UNVERIFIED: {en: "Shared metadata result is uncertain. Use /share-reconcile.", zh: "共享元数据结果尚未确认，请 /share-reconcile 核对。"},
  PUBLISH_COLLABORATION_RESULT_UNVERIFIED: {en: "Platform result is uncertain. Use /platform-reconcile followed by the draft ID.", zh: "平台结果尚未确认，请 /platform-reconcile 草稿ID 核对。"},
  PUBLISH_MERGE_CONFLICT: {en: "The combined result has conflicts. /revise after resolving them, or /work to assign repair work.", zh: "组合结果存在冲突。解决后 /revise，或 /work 指定负责人接手修复。"},
  FORMAL_TEST_REQUIRED: {en: "Final tree prepared. /verify runs the selected tests in isolation.", zh: "已准备最终内容树。/verify 在隔离环境运行指定测试。"},
  FORMAL_CHECKS_FAILED: {en: "Formal checks failed. Fix and /revise, then verify again.", zh: "正式检查失败。修复后 /revise 重新准备，再运行检查。"},
  FORMAL_READY: {en: "Selected checks passed. Review the plan; /execute conditionally updates Formal.", zh: "指定检查通过。核对计划后，/execute 条件更新 Formal。"},
  FORMAL_INTEGRATED: {en: "Local Formal result observed. Remote publication is still pending.", zh: "已核对本地 Formal 结果。远端发布仍待完成。"},
  CHECK_OPERATION_INCOMPLETE: {en: "Use /pause to stop this check, or /reconcile to recover an interrupted check.", zh: "可用 /pause 停止检查；检查中断后用 /reconcile 核对并恢复。"},
  PUBLISH_RUN_QUEUED: {en: "Queued. /pause stops the remaining steps.", zh: "已排队。/pause 停止后续步骤。"},
  PUBLISH_RUN_RUNNING: {en: "Running the authorized steps. /pause stops further work.", zh: "正在推进已授权步骤。/pause 停止后续操作。"},
  PUBLISH_RUN_STOPPING: {en: "Stopping after the active operation is retired.", zh: "正在停止，等待当前操作结束和资源清理。"},
  PUBLISH_RUN_RECONCILIATION_REQUIRED: {en: "Interrupted run. /reconcile observes saved effects without replay.", zh: "连续任务中断。/reconcile 核对已记录效果，不重复执行。"},
  LEGACY_IMPORTED: {en: "Handed off to a stable item; retained here as a historical observation.", zh: "已交接到稳定事项，此处保留历史观察。"},
  PUBLISH_REVIEW_REWORK: {en: "Rework requested. Read the review before integration.", zh: "审阅要求返工，请先查看审阅说明。"},
  PUBLISH_REVIEW_DEFER: {en: "Review deferred this candidate. Integration awaits a new decision.", zh: "审阅已暂缓此候选，合入前需要新的决定。"},
  PAUSE_PENDING: {en: "Pause requested. Waiting for the check and sandbox cleanup to finish.", zh: "已请求暂停，正在等待检查退出与沙箱清理。"},
  PUBLISH_PAUSED: {en: "Paused. /resume enables the next explicit action; it does not start tests or integrate.", zh: "已暂停。/resume 恢复操作入口，之后可明确执行检查或合入。"},
  FIXED_CANDIDATE: {en: "Source commit, base and tree are pinned.", zh: "已固定源提交、基点和内容树。"},
  BOUNDED_CHECKS_ONLY: {en: "Git whitespace check only. Full project verification is still required.", zh: "目前仅执行 Git 空白检查，项目完整验证仍待完成。"},
  CANDIDATE_PREPARED: {en: "Review the candidate, target and owner; /adopt creates the new work branch.", zh: "核对候选、目标与负责人后，可用 /adopt 创建新工作分支。"},
  CHECKS_STALE_PREPARE_REQUIRED: {en: "The target changed. Use /revise to prepare fresh evidence before adopting.", zh: "目标已变化，请用 /revise 重新准备证据后再接手。"},
  CHECKS_FAILED_ADOPTION_AVAILABLE: {en: "Checks failed. Record an owner and remaining work before adopting.", zh: "检查失败。记录接手负责人和剩余工作后仍可接手。"},
  ADOPTED_REWORK_REQUIRED: {en: "Work branch observed. Continue the remaining work; Formal has not been accepted.", zh: "已核对工作分支。继续处理剩余工作；尚未正式接纳。"},
  OPERATION_RECONCILIATION_REQUIRED: {en: "The operation needs reconciliation. /reconcile observes the branch without repeating the write.", zh: "操作结果待核对。/reconcile 只观察分支，不重复写入。"},
  FORMAL_NOT_ACCEPTED: {en: "Adoption does not accept Formal, resolve issues or publish externally.", zh: "接手不会标记正式通过、关闭问题或对外发布。"},
  LEGACY_RECORD_ONLY: {en: "Saved metadata; inspect the recorded flags below.", zh: "这是保存的历史记录，可查看下方原有标志。"},
  LEGACY_CANDIDATE_UNBOUND: {en: "Source commit and target ref must be verified before continuing.", zh: "继续前需要核对固定源提交与目标引用。"},
  RESULT_OID_UNRECORDED: {en: "The resulting Git commit was not recorded; integration is unverified.", zh: "未记录实际结果提交，合入结果尚待核对。"},
  REMOTE_RESULT_UNVERIFIED: {en: "The remote result has not been verified.", zh: "远端结果尚待核对。"},
};
const STATES: Record<string, Copy> = {
  recorded: {en: "Recorded", zh: "已记录"}, needs_reconciliation: {en: "Needs verification", zh: "待核对"},
  unverified: {en: "Unverified", zh: "未验证"},
  prepared: {en: "Prepared", zh: "已准备"}, failed: {en: "Failed", zh: "失败"},
  conflicted: {en: "Conflicts", zh: "存在冲突"},
  passed: {en: "Passed", zh: "通过"}, executing: {en: "Awaiting reconciliation", zh: "等待核对"},
  adopted: {en: "Adopted", zh: "已接手"},
  integrated: {en: "Integrated locally", zh: "已本地合入"}, checking: {en: "Checking", zh: "检查中"},
  stale: {en: "Stale", zh: "已失效"},
  pausing: {en: "Pausing", zh: "暂停中"}, paused: {en: "Paused", zh: "已暂停"},
};
const ERRORS: Record<string, Copy> = {
  PUBLISH_SHARED_CONFLICT: {en: "Shared metadata changed. Prepare a fresh /share with the named remote.", zh: "共享元数据已变化，请 /share 远端名 重新准备。"},
  PUBLISH_ACCOUNT_UNCONFIGURED: {en: "Configure this repository account with /account first.", zh: "请先用 /account 配置此仓库对应的账户。"},
  PUBLISH_PLATFORM_REFRESH_REQUIRED: {en: "Refresh the platform object with /remote first.", zh: "请先用 /remote 刷新平台对象。"},
  PUBLISH_MERGE_CONFLICT: {en: "The combined result has unresolved conflicts.", zh: "组合结果存在未解决的冲突。"},
  PUBLISH_MERGE_UNAVAILABLE: {en: "Git could not produce the merge preview. Check repository history and Git support for the selected method.", zh: "Git 无法生成合并预览，请检查仓库历史及 Git 对所选方式的支持。"},
  PUBLISH_INVALID_SELECTION: {en: "Check the selected commits or paths. Multiple sources require squash or merge.", zh: "请核对所选提交或路径；多个来源需使用 squash 或 merge。"},
  PUBLISH_INVALID_CONTRIBUTIONS: {en: "Supply up to seven additional sources as role | ref.", zh: "最多追加七个来源，格式为角色 | 引用。"},
  PUBLISH_DUPLICATE_CONTRIBUTION: {en: "Two sources identify the same commit.", zh: "两个来源指向同一个提交，请去除重复项。"},
  PUBLISH_UNRELATED_HISTORY: {en: "The candidate and target do not have a supported common history.", zh: "候选与目标缺少可用的共同历史。"},
  PUBLISH_BASE_CHANGED: {en: "The specified base differs from the current merge base. Prepare the selection again.", zh: "指定基点与当前合并基点不符，请重新准备选择。"},
  PUBLISH_BASE_AMBIGUOUS: {en: "This replay needs one unambiguous merge base.", zh: "此重放方式需要唯一合并基点。"},
  PUBLISH_NONLINEAR_SELECTION: {en: "Rebase requires linear history; individual picks require a single parent.", zh: "rebase 需要线性历史，逐个选取的提交必须只有一个父提交。"},
  PUBLISH_OPERATION_RUNNING: {en: "The operation is still running. Use /pause and wait for cleanup before reconciliation.", zh: "操作仍在运行。可用 /pause 暂停，等待清理后再核对。"},
  PUBLISH_RUN_ACTIVE: {en: "A run owns this item. Pause it or reconcile an interrupted run first.", zh: "连续任务占用此事项，请先暂停或核对中断任务。"},
  PUBLISH_INVALID_STEPS: {en: "Use /run verify, /run execute, /run verify execute or /run adopt.", zh: "可用 /run verify、/run execute、/run verify execute 或 /run adopt。"},
  PUBLISH_QUEUE_FULL: {en: "The runtime queue is full. Wait for a pending run to finish.", zh: "运行队列已满，请等待现有任务完成。"},
  PUBLISH_PLAN_REVIEW_REQUIRED: {en: "The plan changed during checks. Review it before authorizing another run.", zh: "检查期间计划发生变化，请审阅后重新指定连续步骤。"},
  PUBLISH_ENVIRONMENT_CHANGED: {en: "The native runtime changed. Verify the final tree again before integration.", zh: "原生运行环境已变化，请重新检查最终树后再合入。"},
  PUBLISH_ENVIRONMENT_UNAVAILABLE: {en: "The native runtime contents could not be verified.", zh: "无法核对原生运行环境的内容。"},
  PUBLISH_LEGACY_SNAPSHOT_CHANGED: {en: "The legacy snapshot changed. Refresh and select the record again.", zh: "旧记录快照已变化，请刷新后重新选择。"},
  PUBLISH_LEGACY_ALREADY_MIGRATED: {en: "This record already has a stable Publish item.", zh: "此旧记录已经关联到稳定 Publish 事项。"},
  PUBLISH_LEGACY_MIGRATED: {en: "Use the stable Publish item; the legacy writer no longer owns this record.", zh: "请使用稳定 Publish 事项，旧入口不再修改这条记录。"},
  PUBLISH_RUNTIME_RECORD_UNAVAILABLE: {en: "The check has no usable recovery record. Keep it pending for runtime inspection.", zh: "缺少可用的检查恢复记录，需要保留待核对状态并检查运行时。"},
  SANDBOX_CLEANUP_PENDING: {en: "Sandbox retirement is not confirmed. Use /reconcile to retry cleanup observation.", zh: "尚未确认沙箱退出，可用 /reconcile 再次核对清理。"},
  PUBLISH_FORMAL_UNCONFIGURED: {en: "Configure the Formal repository first.", zh: "请先配置 Formal 仓库。"},
  PUBLISH_TARGET_UNAVAILABLE: {en: "The explicit target branch could not be read.", zh: "无法读取指定的目标分支。"},
  PUBLISH_TARGET_CHECKED_OUT: {en: "The target is checked out in a worktree. Switch that checkout before preparing.", zh: "目标正被工作区检出，请先切换该工作区再准备。"},
  PUBLISH_TARGET_CHANGED: {en: "The target changed. /revise pins it again and invalidates old checks.", zh: "目标已变化。请 /revise 重新固定目标并检查。"},
  PUBLISH_TARGET_DIVERGED: {en: "This method requires the target to be an ancestor. Resolve the divergence in a work branch first.", zh: "当前方式要求目标是候选的祖先。请先在工作分支解决分叉。"},
  PUBLISH_PLAN_STALE: {en: "Configuration changed. Prepare and verify a new revision.", zh: "配置已变化，请重新准备修订并检查。"},
  PUBLISH_CHECKS_REQUIRED: {en: "Run /verify successfully before /execute.", zh: "先运行 /verify 并通过，再执行 /execute。"},
  PUBLISH_CHECKS_FAILED: {en: "Fix the recorded check failures and prepare a revision first.", zh: "请先修复已记录的检查失败，再准备修订。"},
  PUBLISH_INVALID_TEST_TARGET: {en: "Enter a tracked pytest file or node retained in the final tree.", zh: "请输入最终内容树中保留的 pytest 测试文件或节点。"},
  PUBLISH_INVALID_CHECK_POLICY: {en: "Check entries need unique IDs, valid targets, seeds and time limits.", zh: "检查项需要唯一编号、有效测试入口、随机种子和时限。"},
  PUBLISH_CHECK_BUDGET_EXCEEDED: {en: "The combined check time limits exceed 180 seconds. Shorten the limits or split the candidate.", zh: "合计检查时限超过 180 秒，请缩短时限或拆分候选。"},
  PUBLISH_PLUGIN_MISSING: {en: "A configured commit plugin is missing. Restore it or update project settings.", zh: "缺少已配置的提交插件，请恢复插件或调整项目设置。"},
  PUBLISH_PLUGIN_BLOCKED: {en: "A commit plugin blocked integration. Review its explanation below.", zh: "提交插件阻止了合入，请查看下方原因。"},
  PUBLISH_PLUGIN_LOAD_OR_HOOK_FAILED: {en: "A plugin failed to load or run. Fix it and prepare a new revision.", zh: "插件加载或执行失败，请修复后重新准备修订。"},
  PUBLISH_PLUGIN_HOOK_UNSUPPORTED: {en: "This plugin uses selection or post-sync hooks that are not connected yet.", zh: "此插件包含尚未接入的选择或同步完成钩子。"},
  PUBLISH_PLUGIN_REQUIRES_SQUASH: {en: "Commit plugins currently require squash integration.", zh: "提交插件目前需要使用 squash 集成。"},
  PUBLISH_PLUGIN_PRIVACY_BLOCKED: {en: "The plugin's message failed the outbound privacy check.", zh: "插件生成的消息未通过出站隐私检查。"},
  PUBLISH_PLUGIN_AUTHORSHIP_BLOCKED: {en: "The plugin's message conflicts with the authorship policy.", zh: "插件生成的消息不符合署名策略。"},
  PUBLISH_TEMPLATE_UNAVAILABLE: {en: "The configured commit template is missing.", zh: "缺少已配置的提交模板。"},
  PUBLISH_AUTHORSHIP_TRANSFORM_UNAVAILABLE: {en: "The configured code-comment transformation is not supported by this path yet.", zh: "此路径尚未支持已配置的代码注释清理。"},
  PUBLISH_TREE_UNSUPPORTED: {en: "The tree contains unsupported paths, links or submodules.", zh: "内容树包含尚未支持的路径、链接或子模块。"},
  PUBLISH_TREE_LIMIT: {en: "This candidate exceeds the current file or byte limit.", zh: "候选超出当前文件数量或大小上限。"},
  PUBLISH_HISTORY_LIMIT: {en: "History exceeds the bounded scan. Prepare a smaller range or squash.", zh: "历史超出扫描上限，请缩小范围或准备 squash。"},
  PUBLISH_TEST_UNAVAILABLE: {en: "The native test runtime is unavailable. Inspect its installation before retrying verification.", zh: "原生测试运行时不可用，请检查安装配置后重试检查。"},
  PUBLISH_HISTORY_PRIVACY_BLOCKED: {en: "New history contains blocked private content. Inspect it before preparing another method.", zh: "新增历史包含被阻止的私有内容，请检查后再准备集成。"},
  PUBLISH_FF_REQUIRES_FILTERING: {en: "Fast-forward would retain excluded content. Prepare squash instead.", zh: "fast-forward 会保留被排除的内容，请准备 squash。"},
  PUBLISH_COMMIT_FORMAT_INVALID: {en: "The commit message does not satisfy the configured format.", zh: "提交消息不符合已配置的格式。"},
  PUBLISH_SNAPSHOT_CHANGED: {en: "The list changed. Use /refresh before opening the next page.", zh: "列表已有变化，请先 /refresh 刷新再翻页。"},
  PUBLISH_NO_NEXT_PAGE: {en: "This is the last page.", zh: "已到最后一页。"},
  PUBLISH_UNKNOWN_COMMAND: {en: "Use /prepare, /formal, /combine, /work, /run, /verify, /execute, /decide, /pause, /resume, /plan, /revise, /adopt, /reconcile, /retry, /refresh, /more or /settings.", zh: "可用：/prepare、/formal、/combine、/work、/run、/verify、/execute、/decide、/pause、/resume、/plan、/revise、/adopt、/reconcile、/retry、/refresh、/more、/settings。"},
  PUBLISH_REWORK_REQUIRED: {en: "Failed checks require remaining work. Use /plan.", zh: "检查失败，请用 /plan 填写剩余工作。"},
  PUBLISH_TARGET_EXISTS: {en: "The target already exists. Choose a new branch.", zh: "目标分支已存在，请选择新分支。"},
  PUBLISH_CHECKS_STALE: {en: "Evidence is stale. Use /revise to prepare again.", zh: "证据已失效，请用 /revise 重新准备。"},
  PUBLISH_REVISION_CONFLICT: {en: "The item changed. Refresh before continuing.", zh: "事项已变化，请刷新后继续。"},
  PUBLISH_ACTION_UNAVAILABLE: {en: "This action is unavailable for the selected item.", zh: "当前事项不能执行此操作。"},
  PUBLISH_SOURCE_UNCONFIGURED: {en: "Configure the source role first.", zh: "请先配置来源角色。"},
  PUBLISH_SOURCE_UNAVAILABLE: {en: "The fixed source could not be read.", zh: "无法读取指定来源。"},
  PUBLISH_INVALID_BRANCH: {en: "Enter a valid new branch name.", zh: "请输入有效的新分支名称。"},
  PUBLISH_INVALID_SOURCE: {en: "Use HEAD, a full ref or a full commit ID.", zh: "请使用 HEAD、完整引用或完整提交号。"},
};
// Repository text may contain terminal control sequences. Render it as text,
// never as an instruction to the terminal or as additional fake UI rows.
function plain(value: string) { return value.replace(/[\x00-\x1f\x7f-\x9f]/g, " "); }

export function PublishWorkspacePanel(props: Props) {
  return <PublishWorkspace key={props.project} {...props}/>;
}

function PublishWorkspace(props: Props) {
  const {client, project, onFooter, onDismiss} = props;
  const t = useCopy();
  const {w, h} = usePanelSize({minWidth: 20, widthOffset: 4});
  const input = useTextInput();
  const [tab, setTab] = useState(0);
  const [focus, setFocus] = useState<"header" | "items" | "node" | "settings">("header");
  const [snapshot, setSnapshot] = useState<PublishSnapshot | null>(null);
  const [selected, setSelected] = useState(0);
  const [nodeIndex, setNodeIndex] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<"prepare" | "plan" | "revise" | "formal" | "combine" | "work" | "migrate" | "decide" | null>(null);
  const [operationDraft, setOperationDraft] = useState<PublishOperationMode | null>(null);
  const [sharedPage, setSharedPage] = useState<PublishSharedPage | null>(null);
  const [history, setHistory] = useState<{itemId: string; head: string} | null>(null);
  const auxiliaryRequest = useRef(0);
  const draftItem = useRef<PublishItem | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const pending = useRef<{operation: string; args: Record<string, unknown>; id: string} | null>(null);
  const mounted = useRef(true);
  const mutating = useRef(false);
  const controlling = useRef(false);
  const selection = useRef("");
  const request = useRef(0);
  const currentSnapshot = useRef<PublishSnapshot | null>(null);
  const scroll = useRef<ScrollBoxHandle>(null);
  const detailTop = useRef<DOMElement>(null);
  const pageStart = useRef(0);
  const items = snapshot?.items ?? [];
  const item = items[selected];
  const node = item?.nodes[nodeIndex];
  useEffect(() => { auxiliaryRequest.current++; setSharedPage(null); }, [project, item?.id]);
  const settings = focus === "settings";
  const running = items.some(row => ["queued", "running", "stopping"].includes(row.run?.state ?? ""));
  selection.current = item?.identity_kind === "stable_item" ? item.id : "";

  const load = useCallback(async (cursor = "", preferred = "") => {
    const generation = ++request.current;
    setLoading(true); setError("");
    try {
      const next = await publishItems(client, project, cursor);
      if (generation !== request.current) return;
      const previous = currentSnapshot.current;
      if (cursor && previous && previous.snapshot_id !== next.snapshot_id) throw new Error("PUBLISH_SNAPSHOT_CHANGED");
      const keepSelection = !cursor && pageStart.current === 0 && previous?.snapshot_id === next.snapshot_id;
      currentSnapshot.current = next;
      setSnapshot(next);
      // The initial slice uses snapshot observations, not durable item IDs.
      // Changing a page therefore explicitly selects its first observation.
      const stableIndex = next.items.findIndex(row => row.id === (preferred || selection.current));
      if (stableIndex >= 0) setSelected(stableIndex);
      else if (!keepSelection) { setSelected(0); setNodeIndex(1); }
      pageStart.current = cursor ? pageStart.current + 50 : 0;
    } catch (failure: any) {
      if (generation !== request.current) return;
      setError(String(failure?.code || failure?.message || "PUBLISH_STATE_UNAVAILABLE"));
    } finally {
      if (generation === request.current) setLoading(false);
    }
  }, [client, project]);
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; request.current++; }; }, [load]);
  const execute = async (operation: string, args: Record<string, unknown>, retry = false) => {
    if (mutating.current) return;
    const call = retry ? pending.current : {operation, args, id: randomUUID()};
    if (!call) { setError("PUBLISH_ACTION_UNAVAILABLE"); return; }
    pending.current = call; mutating.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const result = await publishCommand(client, project, call.operation, call.args, call.id);
      if (!mounted.current) return;
      setDraft(null);
      setOperationDraft(null);
      setNotice(result.effect_state === "ambiguous" ? "pending" : "saved");
      pending.current = null;
      await load("", result.item?.id);
    } catch (failure: any) {
      if (!mounted.current) return;
      setError(String(failure?.code || failure?.message || "PUBLISH_OPERATION_UNCERTAIN"));
      if (failure?.effect_state === "not_committed") pending.current = null;
      else { setDraft(null); setOperationDraft(null); }
    } finally {
      mutating.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  useEffect(() => {
    if (!running && (!busy || !["verify", "package"].includes(pending.current?.operation ?? ""))) return;
    const timer = setInterval(() => { void load(); }, 1000);
    return () => clearInterval(timer);
  }, [busy, running, load]);
  const pauseRunning = async () => {
    const active = pending.current;
    if (!active || !["verify", "package"].includes(active.operation) || controlling.current) return;
    controlling.current = true;
    try {
      const latest = await client.callTool("publish.item", {project, item_id: active.args.item_id}) as {item: PublishItem};
      const result = await publishCommand(client, project, "pause", {item_id: latest.item.id, expected_revision: latest.item.revision}, randomUUID());
      if (mounted.current) await load("", result.item?.id);
    } catch (failure: any) {
      if (mounted.current) setError(String(failure?.code || failure?.message || "PUBLISH_OPERATION_UNCERTAIN"));
    } finally { controlling.current = false; }
  };
  const readShared = async (more = false) => {
    if (!item || more && !sharedPage?.next_cursor) { setError("PUBLISH_ACTION_UNAVAILABLE"); return; }
    const generation = ++auxiliaryRequest.current;
    try {
      const result = await client.callTool("publish.shared.items", {project, item_id: item.id, limit: 20,
        ...(more ? {observation_id: sharedPage!.observation_id, cursor: sharedPage!.next_cursor} : {})}) as PublishSharedPage;
      if (!mounted.current || generation !== auxiliaryRequest.current) return;
      if (result.item_id !== item.id || result.schema_version !== 1) throw new Error("PUBLISH_RESPONSE_MISMATCH");
      setSharedPage(result); setFocus("node"); setError(""); scroll.current?.scrollTo(0);
    } catch (failure: any) {
      if (mounted.current && generation === auxiliaryRequest.current) setError(String(failure?.code || failure?.message));
    }
  };
  useLayoutEffect(() => {
    // Resolve the anchor in the same layout pass as the expanded evidence.
    // Numeric scrollTo(0) can be overtaken by ScrollBox's content-growth follow.
    if (focus === "node" && detailTop.current) scroll.current?.scrollToElement(detailTop.current);
    else scroll.current?.scrollTo(0);
  }, [focus, selected, nodeIndex]);
  useEffect(() => {
    if (settings || draft || operationDraft || history) return;
    onFooter({kind: "command", cmdInput: input,
      statusText: t({en: "/prepare · /formal · /run verify execute · /settings", zh: "/prepare 接手准备 · /formal 正式整理 · /run verify execute 连续执行 · /settings 设置"}),
      suggestions: [], suggestionIdx: 0, cmdResult: ""});
    return () => onFooter(null);
  }, [settings, draft, operationDraft, history, input, input.value, input.cursor, onFooter, t]);

  useInput((text, key) => {
    if (input.value.startsWith("/")) {
      if (matchChord("escape", text, key)) input.setValue("");
      else if (matchChord("enter", text, key)) {
        const command = input.value.trim(); input.setValue("");
        if (mutating.current) {
          if (command === "/pause") void pauseRunning();
          else if (command === "/refresh") void load();
          return;
        }
        if (command === "/prepare") { draftItem.current = undefined; setError(""); setDraft("prepare"); }
        else if (command === "/history" || command.startsWith("/history ")) {
          const head = command.split(/\s+/)[1] ?? "candidate";
          if (item?.identity_kind !== "stable_item" || !/^(candidate|formal|contribution:\d+)$/.test(head)) setError("PUBLISH_ACTION_UNAVAILABLE");
          else setHistory({itemId: item.id, head});
        }
        else if (command === "/shared" || command === "/shared-more") void readShared(command === "/shared-more");
        else if (["/remote", "/search", "/edit", "/state", "/comment-edit", "/reply", "/followup", "/comment", "/review", "/issue", "/pull", "/account", "/package", "/export", "/release", "/tag", "/import", "/download", "/link", "/branch"].includes(command)) {
          const mode = command.slice(1).replace("-", "_");
          const capability = ["remote", "search", "edit", "state", "comment_edit", "reply", "followup", "comment", "review", "issue", "pull"].includes(mode) ? "collaborate" : mode === "link" ? "link_task" : mode === "import" ? "import_package" : mode;
          if (command !== "/account" && (item?.identity_kind !== "stable_item" || !item.allowed_actions.includes(capability))) setError("PUBLISH_ACTION_UNAVAILABLE");
          else { draftItem.current = item; setError(""); setOperationDraft(mode as PublishOperationMode); }
        }
        else if (command.startsWith("/share ") || command.startsWith("/share-refresh ") || command === "/share-send" || command === "/share-reconcile") {
          const [name, remote] = command.split(/\s+/);
          const action = name === "/share-send" ? "share_publish" : name === "/share-reconcile" ? "share_reconcile" : "share";
          if (!item?.allowed_actions?.includes(action)) setError("PUBLISH_ACTION_UNAVAILABLE");
          else void execute(action === "share_publish" ? "share.publish" : "share", {item_id: item.id, expected_revision: item.revision,
            ...(action === "share_publish" ? {} : {action: name === "/share" ? "prepare" : name === "/share-refresh" ? "refresh" : "reconcile", ...(remote ? {remote} : {})})});
        }
        else if (command.startsWith("/send ") || command.startsWith("/platform-reconcile ")) {
          const [name, id, page] = command.split(/\s+/);
          const action = name === "/send" ? "collaborate_send" : "collaborate_reconcile";
          if (!item?.allowed_actions?.includes(action)) setError("PUBLISH_ACTION_UNAVAILABLE");
          else void execute(name === "/send" ? "collaborate.send" : "collaborate", {item_id: item.id, expected_revision: item.revision,
            draft_id: id, ...(name === "/send" ? {} : {action: "reconcile", page: Number(page || 1)})});
        }
        else if (command === "/platform-more") {
          const latest = item?.collaboration?.snapshots.at(-1);
          if (!item?.allowed_actions?.includes("collaborate") || !latest) setError("PUBLISH_ACTION_UNAVAILABLE");
            else void execute("collaborate", {item_id: item.id, expected_revision: item.revision, action: latest.query ? "search" : "refresh",
              ...(latest.query ? {query: latest.query} : {}), target: latest.target, resource: latest.resource, page: latest.page + 1});
        }
        else if (command === "/distribution-reconcile") {
          if (!item?.allowed_actions?.includes("distribution_reconcile")) setError("PUBLISH_ACTION_UNAVAILABLE");
          else void execute("distribution_reconcile", {item_id: item.id, expected_revision: item.revision});
        }
        else if (command === "/branch-apply" || command === "/branch-reconcile") {
          const apply = command === "/branch-apply";
          if (!item?.allowed_actions?.includes(apply ? "branch_apply" : "branch_reconcile")) setError("PUBLISH_ACTION_UNAVAILABLE");
          else void execute(apply ? "branch.apply" : "branch", {item_id: item.id, expected_revision: item.revision, ...(apply ? {} : {action: "reconcile"})});
        }
        else if (command === "/run" || command.startsWith("/run ")) {
          const steps = command.split(/\s+/).slice(1);
          if (!steps.length) setError("PUBLISH_INVALID_STEPS");
          else if (!item?.allowed_actions?.includes("run")) setError("PUBLISH_ACTION_UNAVAILABLE");
          else void execute("run", {item_id: item.id, expected_revision: item.revision, steps});
        }
        else if (command === "/formal" || command === "/combine") { draftItem.current = undefined; setError(""); setDraft(command === "/combine" ? "combine" : "formal"); }
        else if (["/plan", "/revise", "/adopt", "/reconcile", "/verify", "/execute", "/pause", "/resume", "/migrate", "/decide", "/work"].includes(command)) {
          const action = command.slice(1);
          if (!item?.allowed_actions?.includes(action)) setError("PUBLISH_ACTION_UNAVAILABLE");
          else if (action === "plan" || action === "revise" || action === "migrate" || action === "decide" || action === "work") { draftItem.current = item; setDraft(action); }
          else void execute(action, {item_id: item.id, expected_revision: item.revision});
        }
        else if (command === "/retry") void execute("", {}, true);
        else if (command === "/refresh") void load();
        else if (command === "/more" && snapshot?.next_cursor && !loading) void load(snapshot.next_cursor);
        else if (command === "/settings") { setTab(1); setFocus("settings"); }
        else setError(command === "/more" ? "PUBLISH_NO_NEXT_PAGE" : "PUBLISH_UNKNOWN_COMMAND");
      } else if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
      else if (matchChord("delete", text, key)) applyTextOp({op: "delete_forward"}, input);
      else if (matchChord("left", text, key)) applyTextOp({op: "move_cursor", delta: -1}, input);
      else if (matchChord("right", text, key)) applyTextOp({op: "move_cursor", delta: 1}, input);
      else if (matchChord("home", text, key)) applyTextOp({op: "move_to_start"}, input);
      else if (matchChord("end", text, key)) applyTextOp({op: "move_to_end"}, input);
      else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
      return;
    }
    if (text === "/") { input.setValue("/"); return; }
    if (matchChord("escape", text, key)) {
      if (sharedPage) { setSharedPage(null); auxiliaryRequest.current++; return; }
      if (focus === "header") onDismiss();
      else setFocus(focus === "node" ? "items" : "header");
      return;
    }
    const delta = matchChord("left", text, key) ? -1 : matchChord("right", text, key) ? 1 : 0;
    if (focus === "header") {
      if (delta) setTab(value => Math.max(0, Math.min(1, value + delta)));
      if (matchChord("enter", text, key) || matchChord("down", text, key)) setFocus(tab === 1 ? "settings" : "items");
    } else if (focus === "items") {
      if (delta && item) setNodeIndex(value => Math.max(0, Math.min(item.nodes.length - 1, value + delta)));
      const move = matchChord("up", text, key) ? -1 : matchChord("down", text, key) ? 1 : 0;
      if (move) { setSelected(value => Math.max(0, Math.min(items.length - 1, value + move))); setNodeIndex(1); }
      if (matchChord("enter", text, key) && node) setFocus("node");
    } else if (focus === "node") {
      if (matchChord("up", text, key)) scroll.current?.scrollBy(-1);
      if (matchChord("down", text, key)) scroll.current?.scrollBy(1);
    }
  }, {isActive: !settings && !draft && !operationDraft && !history});

  if (settings) return <PublishPanel {...props} onDismiss={() => setFocus("header")}/>;
  if (history) return <PublishHistoryPanel client={client} project={project} itemId={history.itemId} head={history.head} onFooter={onFooter} onDismiss={() => setHistory(null)}/>;
  if (operationDraft) return <PublishOperationDraft mode={operationDraft} item={draftItem.current} busy={busy}
    status={error ? t(ERRORS[error] ?? {en: error, zh: error}) : ""} onFooter={onFooter} onDismiss={() => { pending.current = null; setOperationDraft(null); }}
    onSubmit={(operation, args) => {
      if (operation === "package") setOperationDraft(null);
      void execute(operation, {...args, ...(operation === "account.save" ? {} : {item_id: draftItem.current?.id, expected_revision: draftItem.current?.revision})});
    }}/>;
  if (draft) return <PublishDraft mode={draft} item={draftItem.current}
    busy={busy} status={error ? t(ERRORS[error] ?? {en: "Could not save. Check these values.", zh: "保存未完成，请检查这些字段。"}) : ""}
    onFooter={onFooter} onDismiss={() => setDraft(null)} onSubmit={args => {
      void execute(draft === "plan" ? "plan.update" : draft === "migrate" ? "migrate" : draft === "decide" ? "decide" : "prepare", {...args,
        ...(draft === "migrate" ? {legacy_id: draftItem.current?.id}
          : draft !== "prepare" && draftItem.current ? {item_id: draftItem.current.id, expected_revision: draftItem.current.revision} : {})});
    }}/>;
  const visibleCount = Math.max(1, Math.floor((h - 12) / 2));
  const start = Math.max(0, Math.min(selected - Math.floor(visibleCount / 2), items.length - visibleCount));
  const stateName = (value: string) => t(STATES[value] ?? {en: "Unknown", zh: "未知"});
  const nodeName = (value: string) => t(NODE_NAMES[value] ?? {en: "Step", zh: "步骤"});
  const flag = (value: boolean) => value ? t({en: "yes", zh: "是"}) : t({en: "no", zh: "否"});
  return <Box flexDirection="column" paddingX={1} flexGrow={1} flexBasis={0} minHeight={0}>
    <Box flexDirection="column" flexShrink={0}>
    <HorizontalHeaderStrip items={[{id: "items", label: t({en: "Items", zh: "事项"})},
      {id: "settings", label: t({en: "Settings", zh: "设置"})}]} selected={tab} width={w} contentFocused={focus !== "header"}/>
    <Text color={colors.divider.color}>{separator(w)}</Text>
    <Text dimColor wrap="truncate-end">{plain(project)} · {t({en: "Saved local records", zh: "本地已保存记录"})}</Text>
    {loading ? <Text dimColor>{t({en: "Reading records…", zh: "正在读取记录…"})}</Text> : null}
    {busy ? <Text>{t(["verify", "package"].includes(pending.current?.operation ?? "") ? {en: "Checking or building… /pause requests cancellation.", zh: "正在检查或构建… 可用 /pause 请求暂停。"} : {en: "Saving or executing…", zh: "正在保存或执行…"})}</Text> : null}
    {notice ? <Text>{t(notice === "pending" ? {en: "Result uncertain. Open the saved item for its waiting reason.", zh: "结果尚未确认，请查看已保存事项中的等待原因。"}
      : {en: "Saved. The item shows the observed result.", zh: "已保存，事项显示实际核对结果。"})}</Text> : null}
    {error ? <Text>{t(ERRORS[error] ?? {en: "Operation unavailable; records may be stale. /refresh reads state; /retry reuses the request ID.", zh: "操作未完成，已有记录可能过期。/refresh 读取状态；/retry 复用原请求。"})}</Text> : null}
    </Box>
    <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
      {sharedPage ? <Box flexDirection="column">
        <Text bold>{t({en: "Saved shared facts", zh: "已保存的共享事实"})} · {plain(sharedPage.remote)} · {sharedPage.total}</Text>
        <Text>{sharedPage.observed_oid}</Text>
        <Text>{t({en: "This view does not run work or grant permission. Esc returns to the local item.", zh: "浏览不执行任务或授予权限；Esc 返回本地事项。"})}</Text>
        {sharedPage.items.map(row => <Box key={row.id} flexDirection="column" marginTop={1}>
          <Text bold>{plain(row.title)} · {row.id}</Text>
          <Text>{t({en: "Owner / remaining work", zh: "负责人 / 剩余工作"})}: {plain(row.owner)} · {plain(row.rework)}</Text>
          {row.reviews.map(review => <Text key={review.id}>{review.decision} · {plain(review.owner)} · {plain(review.note)}</Text>)}
          {row.result_oids.map(oid => <Text key={oid}>{t({en: "Observed Formal result", zh: "已观察的正式结果"})}: {oid}</Text>)}
          {row.branch_retention?.map(retained => <Text key={retained.id}>{plain(retained.ref)} · {plain(retained.owner)} · {plain(retained.reason)}</Text>)}
          {row.task_links?.map(link => <Text key={link.id}>{t({en: "Task reference", zh: "任务引用"})}: {plain(link.task_id)} · {plain(link.note ?? "")}</Text>)}
        </Box>)}
        {sharedPage.next_cursor ? <Text>{t({en: "/shared-more reads the next saved page.", zh: "/shared-more 读取下一页已保存事实。"})}</Text> : null}
      </Box> : tab === 1 ? <Text>{t({en: "Enter to manage commit format, privacy and remotes.", zh: "Enter 管理提交格式、隐私和远端。"})}</Text>
        : focus === "node" && item && node ? <Box ref={detailTop} flexDirection="column" paddingTop={1}>
          <Text bold>{nodeName(node.id)} · {stateName(node.status)}</Text>
          <Text>{plain(item.title)}</Text>
          <Text>{t(REASONS[node.reason] ?? {en: "Verification required.", zh: "需要核对。"})}</Text>
          {item.identity_kind === "stable_item" ? <Box flexDirection="column">
            <Text>{t({en: "Source commit", zh: "源提交"})}: {item.candidate_oid}</Text>
            <Text>{t({en: "Target branch", zh: "目标分支"})}: {plain(item.target ?? "")}</Text>
            {item.run ? <Box flexDirection="column">
              <Text>{t({en: "Authorized steps", zh: "已指定步骤"})}: {item.run.steps.join(" → ")} · {item.run.state} · {item.run.next}/{item.run.steps.length}</Text>
              <Text>{t({en: "Run owner", zh: "执行身份"})}: {plain(item.run.actor)} {plain(item.run.task_id)}</Text>
              {item.run.reason ? <Text>{t(ERRORS[item.run.reason] ?? REASONS[item.run.reason] ?? {en: item.run.reason, zh: item.run.reason})}</Text> : null}
              {item.run.observations.map((step, index) => <Text key={index}>{step.operation}: {step.effect_state} {plain(step.error ?? "")}</Text>)}
            </Box> : null}
            {item.plan?.destination === "formal" ? <Box flexDirection="column">
              <Text>{t({en: "Method", zh: "方式"})}: {item.plan.method} · {t({en: "Expected target", zh: "预期目标"})}: {item.plan.expected_target_oid}</Text>
              <Text>{t({en: "Final tree", zh: "最终内容树"})}: {item.plan.tree_oid}</Text>
              {item.plan.contributions?.map((source, index) => <Text key={index}>{t({en: "Contribution", zh: "候选来源"})}: {source.role} · {plain(source.source_ref)} · {source.oid}</Text>)}
              {item.plan.conflict_paths?.map(path => <Text key={path}>{t({en: "Conflict", zh: "冲突"})}: {plain(path)}</Text>)}
              {item.plan.paths?.map(path => <Text key={path}>{t({en: "Selected file", zh: "选中文件"})}: {plain(path)}</Text>)}
              {item.plan.commit_series?.map((step, index) => <Text key={index}>{t({en: "Replay", zh: "重放"})}: {step.source_oid.slice(0, 12)} → {plain(step.message)}</Text>)}
              {item.plan.dependencies?.map(dependency => <Text key={dependency.path}>
                {t({en: "Dependency input", zh: "依赖输入"})}: {plain(dependency.path)} · {dependency.sha256}
              </Text>)}
              {item.plan.transformed_files?.map(file => <Text key={file.path}>
                {t({en: "Authorship filter", zh: "署名过滤"})}: {plain(file.path)} · {file.before.slice(0, 12)} → {file.after.slice(0, 12)}
              </Text>)}
              {item.plan.checks ? item.plan.checks.map(check => <Text key={check.id}>
                {t({en: "Required test", zh: "必需测试"})}: {plain(check.target)} · {check.seeds.join(",")} · {check.timeout}s
              </Text>) : <Text>{t({en: "Selected test", zh: "指定测试"})}: {plain(item.plan.test_target ?? "")}</Text>}
              <Text>{t({en: "Private files excluded", zh: "已排除私有文件"})}: {item.plan.excluded_path_fingerprints?.length ?? 0}</Text>
              {item.plan.plugins?.map(plugin => <Text key={plugin.id}>{t({en: "Commit plugin", zh: "提交插件"})}: {plain(plugin.id)} · {plugin.digest.slice(0, 12)}</Text>)}
              {item.plan.commit_message ? <Text>{t({en: "Commit message", zh: "提交消息"})}: {plain(item.plan.commit_message)}</Text> : null}
            </Box> : null}
            <Text>{t({en: "Owner", zh: "负责人"})}: {plain(item.owner ?? "")}</Text>
            <Text>{t({en: "Remaining work", zh: "剩余工作"})}: {plain(item.rework || t({en: "Not recorded", zh: "未记录"}))}</Text>
            {item.legacy_source ? <Text>{t({en: "Legacy backup", zh: "旧记录备份"})}: {item.legacy_source.record.prefix}-{item.legacy_source.record.number} · {item.legacy_source.backup_ref}</Text> : null}
            {item.plugin_completion ? <Text>{t({en: "Completion plugins", zh: "完成插件"})}: {item.plugin_completion.state} · {item.plugin_completion.git_result_oid}</Text> : null}
            {item.branches ? <Box flexDirection="column">
              {item.branches.retention.map(row => <Text key={row.id}>{t({en: "Branch retained", zh: "分支保留"})}: {plain(row.ref)} · {row.oid} · {plain(row.owner)} · {plain(row.reason)}</Text>)}
              {item.branches.plan ? <Box flexDirection="column">
                <Text bold>{t({en: "Branch change", zh: "分支变更"})}: {item.branches.plan.kind} · {item.branches.plan.state}</Text>
                <Text>{plain(item.branches.plan.branch)} · {item.branches.plan.expected_oid}</Text>
                {item.branches.plan.archive_ref ? <Text>{t({en: "Recovery reference", zh: "恢复引用"})}: {item.branches.plan.archive_ref}</Text> : null}
                {item.branches.plan.observation ? <Text>{plain(item.branches.plan.observation.remote)} · {plain(item.branches.plan.observation.ref)} · {item.branches.plan.observation.oid ?? t({en: "New branch", zh: "新分支"})}</Text> : null}
                {item.allowed_actions.includes("branch_apply") ? <Text>{t({en: "/branch-apply executes this prepared change.", zh: "/branch-apply 执行此已准备变更。"})}</Text> : null}
              </Box> : null}
            </Box> : null}
            {item.packages?.map(pkg => <Box key={pkg.id} flexDirection="column">
              <Text bold>{t({en: "Version assets", zh: "版本产物"})}: {plain(pkg.version)} · {pkg.id}</Text>
              <Text>{t({en: "Fixed source", zh: "固定源码"})}: {pkg.source_oid} · {pkg.tree_oid}</Text>
              {pkg.assets.map(asset => <Text key={asset.name}>{plain(asset.name)} · {asset.size} B · SHA-256 {asset.sha256}</Text>)}
            </Box>)}
            {item.distribution_operation ? <Text>{t({en: "Distribution", zh: "分发"})}: {item.distribution_operation.kind} · {item.distribution_operation.state} · {plain(item.distribution_operation.reason ?? "")}</Text> : null}
            {item.distribution_operation?.build_environment ? <Text>{item.distribution_operation.build_environment.platform} · {item.distribution_operation.build_environment.architecture} · {item.distribution_operation.build_environment.digest}</Text> : null}
            {item.distribution_operation?.build_log ? <Text>{plain(item.distribution_operation.build_log)}</Text> : null}
            {item.package_imports?.map(row => <Text key={row.id}>{t({en: "Imported and verified", zh: "导入并验真"})}: {plain(row.version)} · {row.sha256} · {row.verification}</Text>)}
            {item.task_links?.map(row => <Text key={row.id}>{t({en: "Linked task", zh: "关联任务"})}: {plain(row.task_id)} · {plain(row.parent_task_id ?? "")} · {plain(row.note ?? "")}</Text>)}
            {item.sharing ? <Box flexDirection="column">
              <Text bold>{t({en: "Shared metadata", zh: "共享元数据"})}: {item.sharing.state} · {plain(item.sharing.observation.remote)}</Text>
              <Text>{plain(item.sharing.preview.title)} · {plain(item.sharing.preview.owner)} · {plain(item.sharing.preview.rework)}</Text>
              <Text>{item.sharing.result_oid} · SHA-256 {item.sharing.sha256}</Text>
              {item.allowed_actions.includes("share_publish") ? <Text>{t({en: "/share-send publishes this reviewed snapshot.", zh: "/share-send 发布此已准备快照。"})}</Text> : null}
            </Box> : null}
            {item.shared_observations?.length ? <Text>{t({en: "/shared opens the latest saved shared observation; /share-refresh remote explicitly updates it.", zh: "/shared 查看最近的共享观察；/share-refresh 远端名 显式更新。"})}</Text> : null}
            {item.collaboration?.snapshots.slice(-1).map(snapshot => <Box key={snapshot.id} flexDirection="column">
              <Text bold>{t({en: "Platform observation", zh: "平台观察"})}: {plain(snapshot.target.instance)} / {plain(snapshot.target.repository)} · {plain(snapshot.target.account)} · {snapshot.page}</Text>
              {snapshot.document ? <Text>{plain(snapshot.document.title ?? "")} · {plain(snapshot.document.state ?? "")}{"\n"}{plain(snapshot.document.body ?? "")}</Text> : null}
              {[...(snapshot.documents ?? []), ...(snapshot.comments ?? []), ...(snapshot.reviews ?? []), ...(snapshot.anchors ?? [])].map((record, index) => <Text key={index}>
                {record.id} · {plain(record.author?.login ?? "")} · {plain(record.title ?? "")} · {plain(record.state ?? "")} {plain(record.body ?? "")}
                {record.anchor ? ` · ${plain(record.anchor.path ?? record.anchor.new_path ?? "")}:${record.anchor.line ?? record.anchor.new_line ?? ""}` : ""}
              </Text>)}
              {snapshot.more || snapshot.comments_more || snapshot.reviews_more || snapshot.anchors_more || snapshot.files_more ? <Text>{t({en: "/platform-more reads the next page explicitly.", zh: "/platform-more 显式读取下一页。"})}</Text> : null}
            </Box>)}
            {item.collaboration?.drafts.map(draft => <Box key={draft.id} flexDirection="column">
              <Text bold>{t({en: "Platform draft", zh: "平台草稿"})}: {draft.id} · {draft.state} {plain(draft.reason ?? "")}</Text>
              <Text>{plain(draft.target.instance)} / {plain(draft.target.repository)} · {plain(draft.target.account)} · {draft.resource.kind} {draft.resource.number ?? ""}</Text>
              <Text>{plain(draft.content.title ?? "")}{"\n"}{plain(draft.content.body)}</Text>
              <Text>{draft.content.kind} {draft.content.state ?? ""} {draft.content.reference ? `${draft.content.reference.kind}:${draft.content.reference.id}` : ""}</Text>
              {draft.binding !== item.binding ? <Text>{t({en: "Stale candidate", zh: "候选已变化，草稿失效"})}</Text> : null}
            </Box>)}
            {item.collaboration?.followups?.filter(row => row.binding === item.binding).map(row => <Box key={row.id} flexDirection="column">
              <Text>{t({en: "Reply follow-up", zh: "回复跟进"})}: {plain(row.resource.kind)} {row.resource.number ?? ""} · {plain(row.reference.kind)}:{plain(row.reference.id)} · {row.status === "resolved" ? t({en: "Locally resolved", zh: "本地已处理"}) : t({en: "Awaiting reply", zh: "待回复"})}</Text>
              <Text>{plain(row.owner)} · {plain(row.note)} · {row.recorded_at}</Text>
            </Box>)}
            {item.reviews?.map(review => <Box key={review.id} flexDirection="column">
              <Text>{t({en: "Review", zh: "审阅"})}: {review.decision} · {plain(review.owner)}{review.binding !== item.binding ? t({en: " · stale", zh: " · 已失效"}) : ""}</Text>
              <Text>{plain(review.note)}</Text>
            </Box>)}
            {item.evidence?.map(evidence => <Box flexDirection="column" key={evidence.id}>
              <Text>{t(evidence.kind === "project_test" ? {en: "Selected project tests", zh: "指定项目测试"}
                : evidence.kind === "commit_plugins" ? {en: "Commit plugin checks", zh: "提交插件检查"}
                : evidence.kind === "environment" ? {en: "Native runtime and dependencies", zh: "原生环境与依赖"}
                : evidence.kind === "merge_preview" ? {en: "Combined merge preview", zh: "组合合并预览"}
                : evidence.kind === "outbound_privacy" ? {en: "Outbound privacy", zh: "出站隐私检查"}
                : {en: "Git whitespace check", zh: "Git 空白检查"})} · {stateName(evidence.outcome)}
                {evidence.binding !== item.binding ? t({en: " · stale", zh: " · 已失效"})
                  : evidence.current === false ? t({en: " · superseded", zh: " · 已有后续检查"}) : ""}</Text>
              {evidence.summary ? <Text dimColor>{ERRORS[evidence.summary] ? t(ERRORS[evidence.summary]) : plain(evidence.summary)}</Text> : null}
              {evidence.detail ? <Text dimColor>{plain(evidence.detail)}</Text> : null}
              {evidence.environment ? <Text dimColor>{evidence.environment.platform} · {evidence.environment.file_count} {t({en: "runtime files", zh: "个运行环境文件"})} · {evidence.environment.digest}</Text> : null}
              {evidence.checks?.map((result, index) => <Box key={result.check.id ?? index} flexDirection="column">
                <Text>{plain(result.check.target)} · {result.state === "not_run" ? t({en: "Not run", zh: "未运行"}) : stateName(result.passed ? "passed" : "failed")}</Text>
                {result.error ? <Text dimColor>{plain(result.error)}</Text> : null}
                {result.seed_results?.map(seed => <Text key={seed.seed} dimColor>{seed.seed}: {stateName(seed.exit_code === 0 ? "passed" : "failed")} · {seed.duration_ms}{t({en: "ms", zh: "毫秒"})} · {plain(seed.output_tail)}</Text>)}
              </Box>)}
            </Box>)}
            {item.operations?.map(operation => <Text key={operation.id} dimColor>{t({en: "Observed result", zh: "已核对结果"})}: {operation.result_oid || t({en: "Pending", zh: "待核对"})}</Text>)}
            <Text>{t({en: "Available actions:", zh: "可用操作："})} {item.allowed_actions.map(action => ACTION_COMMANDS[action] ?? "/" + action).join(" · ")}</Text>
            <Text>{t({en: "/history [candidate|formal|contribution:N] reads fixed Git parents and differences.", zh: "/history [candidate|formal|contribution:N] 查看固定提交历史与差异。"})}</Text>
          </Box> : <Box flexDirection="column">
          <Text dimColor>{t({en: "Recorded sync flag", zh: "原记录同步标志"})}: {flag(item.recorded.synced)}</Text>
          <Text dimColor>{t({en: "Recorded push flag", zh: "原记录推送标志"})}: {flag(item.recorded.pushed)}</Text>
          <Text dimColor>{t({en: "Owner", zh: "负责人"})}: {t({en: "Not recorded", zh: "未记录"})}</Text>
          <Text dimColor>{t({en: "Source commit and target", zh: "源提交与目标"})}: {t({en: "Not bound", zh: "未绑定"})}</Text>
          {item.source.state_ref ? <Text dimColor wrap="wrap">{t({en: "Record reference", zh: "记录引用"})}: {item.source.state_ref}</Text> : null}
          <Text>{t({en: "These flags are historical metadata. A verified Git result is still required before continuing.", zh: "以上标志来自历史记录，继续操作前仍需核对实际 Git 结果。"})}</Text>
          {item.allowed_actions?.includes("migrate") ? <Text>{t({en: "/migrate backs up this record and prepares an explicitly chosen candidate.", zh: "/migrate 备份此旧记录，并准备明确选择的候选。"})}</Text> : null}
          </Box>}
        </Box> : items.length ? <Box flexDirection="column" paddingTop={1}>
          {items.slice(start, start + visibleCount).map((row, offset) => {
            const active = start + offset === selected;
            return <Box key={row.id} flexDirection="column">
              <Text color={active ? colors.selection.row.fg : undefined} bold={active} wrap="truncate-end">
                {active ? "› " : "  "}{plain(row.label)} {truncate(plain(row.title), Math.max(8, w - 16))}
              </Text>
              {active ? <Box flexDirection="column">
                <Text>{stateName(row.status)} · {t(REASONS[row.reason] ?? {en: "Verify source and target", zh: "下一步：核对来源与目标"})}</Text>
                <Box flexDirection="row" flexWrap="wrap">{row.nodes.map((step, index) => <Text key={step.id}
                  bold={index === nodeIndex} color={index === nodeIndex ? colors.selection.row.fg : undefined}>
                  {index ? " → " : ""}{index === nodeIndex ? "[" : ""}{nodeName(step.id)}{index === nodeIndex ? "]" : ""}
                </Text>)}</Box>
              </Box> : null}
            </Box>;
          })}
          <Text dimColor>{pageStart.current + selected + 1}/{snapshot?.total ?? 0}{snapshot?.next_cursor ? t({en: " · /more for next page", zh: " · /more 下一页"}) : ""}</Text>
        </Box> : !loading && !error ? <Text>{t({en: "No saved items. /prepare pins a committed source; uncommitted changes are not included.", zh: "尚无保存事项。/prepare 固定已提交来源；未提交变更不包含在内。"})}</Text> : null}
    </ScrollBox>
    <Box flexShrink={0}><Text dimColor wrap="wrap">{focus === "header" ? t({en: "←/→ section · Enter open · Esc back", zh: "←/→ 栏目 · Enter 进入 · Esc 返回"})
      : focus === "node" ? t({en: "↑/↓ scroll · Esc return to item", zh: "↑/↓ 滚动 · Esc 返回事项"})
      : t({en: "↑/↓ item · ←/→ step · Enter details · Esc header", zh: "↑/↓ 事项 · ←/→ 步骤 · Enter 详情 · Esc 页头"})}</Text></Box>
  </Box>;
}
