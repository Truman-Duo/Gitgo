import type {PublishItem} from "../backend/publish.js";
import type {Suggestion} from "./CommandBar.js";
import type {Copy} from "../ui/pageContract.js";

type Definition = {name: string; title: Copy; action?: string; stable?: boolean};
const definitions: Definition[] = [
  {name: "prepare", title: {en: "Prepare a fixed candidate", zh: "准备固定候选"}},
  {name: "formal", title: {en: "Prepare Formal integration", zh: "准备正式整理"}},
  {name: "combine", title: {en: "Combine candidates", zh: "组合多个候选"}},
  {name: "settings", title: {en: "Publish settings", zh: "发布设置"}},
  {name: "refresh", title: {en: "Read current state", zh: "刷新实际状态"}},
  {name: "account", title: {en: "Manage platform accounts", zh: "管理平台账户"}},
  ...([ ["plan", "调整接手计划"], ["revise", "修订正式计划"], ["adopt", "接手继续完善"],
    ["reconcile", "核对未知结果"], ["verify", "运行检查"], ["execute", "执行已准备计划"],
    ["pause", "请求暂停"], ["resume", "恢复流程"], ["migrate", "迁移旧记录"],
    ["decide", "审阅固定候选"], ["work", "返回加工"], ["package", "准备版本包"],
    ["export", "导出产物"], ["release", "准备分发"], ["tag", "准备版本标签"],
    ["download", "下载并校验"], ["branch", "分支生命周期"] ] as const)
    .map(([name, zh]) => ({name, action: name, title: {en: name, zh: zh}})),
  ...([ ["remote", "查看平台对象"], ["search", "搜索平台对象"], ["edit", "准备对象编辑草稿"],
    ["state", "准备状态变更草稿"], ["comment-edit", "准备评论编辑草稿"], ["reply", "准备回复草稿"],
    ["followup", "记录后续处理"], ["comment", "准备评论草稿"], ["review", "准备审查草稿"],
    ["issue", "准备问题草稿"], ["pull", "准备合并请求草稿"], ["platform-more", "下一页平台对象"] ] as const)
    .map(([name, zh]) => ({name, action: "collaborate", title: {en: `Platform: ${name}`, zh: zh}})),
  ...([ ["link", "link_task", "关联现有任务"], ["import", "import_package", "导入版本包"],
    ["share", "share", "准备共享快照：/share 远端名"], ["share-refresh", "share", "刷新共享观察：/share-refresh 远端名"],
    ["share-send", "share_publish", "发送已准备共享快照"], ["share-reconcile", "share_reconcile", "核对共享发送结果"],
    ["send", "collaborate_send", "发送指定草稿：/send 草稿ID"],
    ["platform-reconcile", "collaborate_reconcile", "核对草稿发送：/platform-reconcile 草稿ID"],
    ["distribution-reconcile", "distribution_reconcile", "核对版本分发结果"],
    ["branch-apply", "branch_apply", "执行已审阅的分支计划"], ["branch-reconcile", "branch_reconcile", "核对分支操作结果"],
    ["run", "run", "指定连续步骤，例如 /run verify execute"] ] as const)
    .map(([name, action, zh]) => ({name, action, title: {en: name, zh: zh}})),
  {name: "history", stable: true, title: {en: "Git branch graph and diff", zh: "Git 分支图与差异"}},
  {name: "preview", stable: true, title: {en: "Combined changes in the prepared plan", zh: "查看已准备的组合变更"}},
  {name: "flow", stable: true, title: {en: "Role flow and processing steps", zh: "三角色流转与处理步骤"}},
  {name: "task", stable: true, title: {en: "Open linked task", zh: "查看关联任务"}},
  {name: "shared", stable: true, title: {en: "Read saved shared facts", zh: "查看共享事实"}},
  {name: "shared-more", stable: true, title: {en: "Next shared page", zh: "下一页共享事实"}},
];

export type PublishCommandContext = {item?: PublishItem; busy?: boolean; more?: boolean; retry?: boolean};
/** Discovery and dispatch admission share the same live backend action projection.
 * This is an interaction guard; the backend remains the authorization authority. */
export function publishCommands(context: PublishCommandContext): Definition[] {
  if (context.busy) return definitions.filter(row => ["pause", "refresh"].includes(row.name));
  const rows = definitions.filter(row => (!row.stable || context.item?.identity_kind === "stable_item")
    && (!row.action || context.item?.allowed_actions?.includes(row.action))
    && (row.name !== "task" || Boolean(context.item?.task_links?.length)));
  if (context.more) rows.push({name: "more", title: {en: "Next item page", zh: "下一页事项"}});
  const preview = rows.findIndex(row => row.name === "preview");
  if (preview >= 0 && !(context.item?.plan?.tree_oid || context.item?.plan?.conflict_preview_tree)) rows.splice(preview, 1);
  if (context.retry) rows.push({name: "retry", title: {en: "Retry saved request", zh: "复用原请求重试"}});
  return rows;
}

export function publishSuggestions(context: PublishCommandContext, query: string, t: (copy: Copy) => string): Suggestion[] {
  if (!query.startsWith("/")) return [];
  return publishCommands(context).filter(row => ("/" + row.name).startsWith(query.trim()))
    .sort((a, b) => Number("/" + b.name === query.trim()) - Number("/" + a.name === query.trim()))
    .map(row => ({label: "/" + row.name, description: t(row.title), inputMode: "fill"}));
}
