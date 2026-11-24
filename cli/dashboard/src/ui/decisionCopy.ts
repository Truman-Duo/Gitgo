import type {PendingDecision} from "../types.js";
import type {Copy} from "./pageContract.js";

type TextField = "label" | "description" | "principle" | "immediate_effect" | "downstream_effect" | "risks" | "reversibility";
type Fields = Partial<Record<TextField, Copy>>;
type Translate = (copy: Copy) => string;

/** Runtime explicitly identifies its fixed templates. A legacy permission
 * contract has a typed request plus its complete known action set. Other
 * ordinary model/user text and unknown evidence remain verbatim. This projection
 * never leaves the renderer or changes the canonical answer/action binding.
 */
export function projectRuntimeDecision(source: PendingDecision, t: Translate): PendingDecision {
  const legacyPermission = !source.display_template && source.kind === "permission" && source.permission_request
    && source.options.length >= 2 && source.options.length <= 3
    && new Set(source.options.map(option=>option.action)).size === source.options.length
    && source.options.some(option => option.action === "allow_once")
    && source.options.some(option => option.action === "deny")
    && source.options.every(option => ["allow_once", "allow_task", "deny"].includes(option.action || ""));
  const options = (copies: Record<string, Fields>) => source.options.map(option => {
    const fields = copies[option.action || ""];
    if (!fields) return option;
    const result = {...option};
    for (const field of Object.keys(fields) as TextField[]) result[field] = t(fields[field]!);
    return result;
  });
  if (source.display_template === "task_execution_policy" && source.display_context?.parameters) {
    const summarize = (policy: any) => {
      if (!policy || policy.status === "disabled" || policy.status === "deleted") return t({en:"No active rules",zh:"没有生效规则"});
      const scope = policy.include_children ? t({en:"This task and subtasks",zh:"当前任务及子任务"}) : t({en:"This task only",zh:"仅当前任务"});
      const tools = policy.allow_only === null ? t({en:"Default tool range",zh:"默认工具范围"})
        : policy.allow_only.join(", ") || t({en:"No business tools",zh:"无业务工具"});
      const actions: Record<string, Copy> = {deny:{en:"Deny",zh:"拒绝"},ask:{en:"Ask first",zh:"执行前确认"},manual_exempt:{en:"Skip MANUAL advancement",zh:"免 MANUAL 推进确认"}};
      const range = policy.allow_only !== null && policy.rules.length
        ? [t({en:"Rules are evaluated within the allowed tool range.",zh:"以下规则在允许的工具范围内匹配。"})] : [];
      return [scope, tools, ...range, ...policy.rules.map((rule: any) => `${rule.tool} · ${t(actions[rule.action])}`
        + (rule.arguments && Object.keys(rule.arguments).length ? ` · ${JSON.stringify(rule.arguments)}` : ""))].join("\n");
    };
    const before = summarize(source.display_context.previous_parameters), next = summarize(source.display_context.parameters);
    const example = source.display_context.match_example as {tool: string; arguments: Record<string, unknown> | null} | undefined;
    const match = !example ? "" : example.arguments ? `${example.tool} ${JSON.stringify(example.arguments)}`
      : `${example.tool} ${t({en:"(any arguments)",zh:"（任意参数）"})}`;
    const unavailable = (source.display_context.not_mounted_tools as string[] | undefined)?.join(", ");
    const exampleLine = match ? t({en:`\nMatching example · ${match}`,zh:`\n匹配示例 · ${match}`}) : "";
    const availabilityLine = unavailable ? t({
      en:`\nUnavailable on the current process · ${unavailable}. Applies if mounted in the selected task scope.`,
      zh:`\n当前进程未挂载 · ${unavailable}。在所选任务范围内挂载后适用。`,
    }) : "";
    const preview = next + exampleLine + availabilityLine;
    return {...source, question:t({en:`Apply these execution rules?\n\nCurrent\n${before}\n\nProposed\n${preview}`,
      zh:`是否应用这组执行规则？\n\n当前\n${before}\n\n拟生效\n${preview}`}),
      why_user_must_decide:t({en:"Confirm how your request applies to these tools.",zh:"确认对话里的要求如何落实到这些工具。"}),
      options:options({apply:{label:{en:"Apply rules",zh:"应用规则"}},cancel:{label:{en:"Keep current rules",zh:"保留当前规则"}}})};
  }
  if ((source.display_template === "permission" || legacyPermission) && source.permission_request) {
    const permission = source.permission_request;
    const tool = permission.tool_name || "", target = permission.resource || "", effect = permission.effect || "";
    const pinned = permission.approval_scope === "exact_read_object_and_arguments";
    return {...source,
      question: t({en:`Allow Gitgo to ${permission.purpose || ""}?`,zh:`是否允许 Gitgo ${permission.purpose || ""}？`}),
      why_user_must_decide: legacyPermission
        ? t({en:"This operation requires your explicit approval for the displayed tool, resource and scope.",zh:"此操作需要你对所示工具、资源和访问范围作出明确授权。"})
        : source.display_context?.read_approval
        ? t({en:"This tool requires approval for the displayed file read.",zh:"该工具读取所示文件需要你的授权。"})
        : t({en:"This operation reaches beyond the Agent's current project scope. Gitgo cannot expand that scope without your explicit approval.",zh:"此操作超出 Agent 当前项目范围，Gitgo 需要你的明确授权才能扩大访问范围。"}),
      options: options({
        allow_once: {
          label:{en:"Allow once",zh:"仅允许一次"},
          principle:{en:"Grant only this exact invocation.",zh:"仅授权本次明确的调用。"},
          immediate_effect:{en:`Gitgo may use ${tool} on ${target} once.`,zh:`允许 Gitgo 对 ${target} 使用 ${tool} 一次。`},
          downstream_effect:{en:"A later invocation must ask again.",zh:"后续调用仍须重新申请。"},
          risks:{en:`The operation has effect=${effect} on the stated resource.`,zh:`此操作会对所示资源执行 ${effect} 行为。`},
          reversibility:{en:"The grant expires after one attempted invocation.",zh:"尝试调用一次后，该授权即失效。"},
        },
        allow_task: {
          label:{en:"Allow for this task",zh:"在本任务内允许"},
          principle:pinned
            ? {en:"Repeat this exact file read with the same arguments until this task ends or 30 minutes elapse.",zh:"仅允许以相同参数重复读取同一文件，直至任务结束或 30 分钟后失效。"}
            : {en:"Grant the same tool and exact resource until this task ends or 30 minutes elapse.",zh:"仅授权同一工具访问明确的同一资源，直至任务结束或 30 分钟后失效。"},
          immediate_effect:pinned
            ? {en:"The same file object may be read again; replacement requires a new approval.",zh:"可再次读取同一文件对象；文件被替换后须重新授权。"}
            : {en:`Repeated ${tool} calls may use ${target}.`,zh:`本任务可重复使用 ${tool} 访问 ${target}。`},
          downstream_effect:{en:"The grant is not inherited by other tasks or processes.",zh:"其他任务或进程不会继承此授权。"},
          risks:pinned
            ? {en:"Normal edits to the same file can change the contents returned by later reads.",zh:"同一文件的正常编辑可能改变后续读取返回的内容。"}
            : {en:"Repeated access within the displayed scope is possible.",zh:"可能在所示范围内重复访问。"},
          reversibility:{en:"Stop the task to revoke the remaining grant.",zh:"停止任务即可撤销剩余授权。"},
        },
        deny: {
          label:{en:"Deny",zh:"拒绝"},
          principle:{en:"Keep the current project boundary unchanged.",zh:"保持当前项目访问边界。"},
          immediate_effect:{en:"The requested operation will not run.",zh:"不会执行所申请的操作。"},
          downstream_effect:{en:"Gitgo must adjust the plan or report the limitation.",zh:"Gitgo 需要调整方案或说明限制。"},
          risks:{en:"The requested task may remain incomplete.",zh:"所请求的任务可能无法完成。"},
          reversibility:{en:"A new, narrower permission can be requested later.",zh:"之后仍可申请范围更小的新授权。"},
        },
      }),
    };
  }
  if (source.display_template === "context_force_compaction" || source.display_template === "context_compaction_maintenance") {
    const maintenance = source.display_template === "context_compaction_maintenance";
    return {...source,
      question:t({en:"Ordinary compaction failed three times. Force a lossy compaction?",zh:"常规压缩已失败三次，是否强制进行有损压缩？"}),
      why_user_must_decide:t({en:"The Host can preserve the full prior epoch in the local audit archive, but must delete part of the provider-visible history to continue.",zh:"完整旧上下文仍会保留在本地审计归档中，但继续运行需要删去部分模型可见历史。"}),
      options:options({
        force_compact:{
          label:{en:"Force compact",zh:"强制压缩"},
          principle:{en:"Keep immutable policy/task prefixes and a bounded host checkpoint.",zh:"保留不可变的规则、任务前缀和有界运行时检查点。"},
          immediate_effect:{en:"Delete older provider-visible history and retry in a new epoch.",zh:"删去较早的模型可见历史，并在新上下文中重试。"},
          downstream_effect:{en:"The Agent continues with less conversational detail; the audit archive remains intact.",zh:"Agent 将基于较少的对话细节继续；审计归档保持完整。"},
          risks:{en:"Fine-grained details not captured by the checkpoint may need to be reopened from durable records.",zh:"未被检查点保留的细节可能需要重新读取持久记录。"},
          reversibility:{en:"The provider context cannot be expanded in place, but the archived epoch remains inspectable.",zh:"无法原地扩回已压缩的模型上下文，但仍可检查旧上下文归档。"},
        },
        stop:{
          label:{en:"Stop safely",zh:"安全停止"},
          principle:{en:"Do not discard model-visible history without approval.",zh:"未经授权不丢弃模型可见历史。"},
          immediate_effect:maintenance
            ? {en:"Cancel compaction and leave the conversation unchanged.",zh:"取消本次压缩，保持会话不变。"}
            : {en:"Stop the current task before another provider request.",zh:"在下一次模型请求前停止当前任务。"},
          downstream_effect:maintenance
            ? {en:"You can correct provider limits or retry /compact later.",zh:"之后可修正模型容量配置，或重新执行 /compact。"}
            : {en:"The task can be resumed after correcting the configured limit or reviewing the archive.",zh:"修正容量配置或检查归档后，可继续此任务。"},
          risks:maintenance
            ? {en:"The existing context may still exceed the provider limit.",zh:"现有上下文仍可能超过模型容量限制。"}
            : {en:"Current work remains incomplete.",zh:"当前工作仍未完成。"},
          reversibility:{en:"A later task can continue from durable state.",zh:"后续任务可从持久状态继续。"},
        },
      }),
    };
  }
  if (source.display_template === "completion_exception") {
    const partial = source.options.some(option => option.action === "accept_partial");
    const change: Fields = {
      label:partial?{en:"Accept partial result",zh:"接受部分结果"}:{en:"Change scope",zh:"调整范围"},
      principle:partial?{en:"Preserve the failed facts and accept an explicitly degraded delivery.",zh:"保留失败事实，并明确接受降级交付。"}:{en:"Change the requested scope instead of pretending a required fact exists.",zh:"明确调整任务范围，保留必需证据尚未满足的事实。"},
      immediate_effect:partial?{en:"Finish this task as degraded with every unresolved gate recorded.",zh:"以降级结果结束任务，并保留所有未满足的检查。"}:{en:"Stop this execution and return the preserved work for a new scope request.",zh:"停止本次执行，保留工作结果，等待新的范围要求。"},
      downstream_effect:partial?{en:"History and reports distinguish it from a verified completion.",zh:"历史与报告会明确区分降级交付和验证完成。"}:{en:"Submit a concrete new task scope; this decision does not waive a safety requirement.",zh:"需要明确提出新任务范围；此选择不会免除安全要求。"},
      risks:{en:"Some requested verification or deliverables remain incomplete.",zh:"部分要求的验证或交付物仍未完成。"},
      reversibility:{en:"A later task can satisfy the preserved gates.",zh:"后续任务仍可补齐保留的检查。"},
    };
    return {...source,
      question:t({en:"The task still cannot pass the same completion checks. How should Gitgo proceed?",zh:"任务仍无法通过相同的完成检查，接下来应如何处理？"}),
      why_user_must_decide:t({en:"The Host supplied the exact checklist and one bounded recovery attempt did not change it. Continuing automatically would waste model work.",zh:"运行时已提供具体检查清单，一次有界恢复仍未解决问题；继续自动运行会重复消耗模型工作。"}),
      options:options({
        continue:{
          label:{en:"Continue and fix",zh:"继续修复"},
          principle:{en:"Keep the current scope and satisfy the Host facts.",zh:"保留当前范围，补齐实际所需的证据。"},
          immediate_effect:{en:"Resume the same process with the checklist attached.",zh:"向原进程提供检查清单并继续执行。"},
          downstream_effect:{en:"Completion remains fully verified if all gates pass.",zh:"全部检查通过后仍可交付完整验证结果。"},
          risks:{en:"This may consume more time and provider budget.",zh:"可能消耗更多时间和模型预算。"},
          reversibility:{en:"You can stop or accept a partial result later.",zh:"之后仍可停止，或在允许时接受部分结果。"},
        },
        accept_partial:change,amend_scope:change,
        stop:{
          label:{en:"Stop safely",zh:"安全停止"},
          principle:{en:"Do not spend more work on an unsatisfied contract.",zh:"停止继续消耗未满足要求的任务。"},
          immediate_effect:{en:"The Host stops this task without another model call and preserves its incomplete result.",zh:"运行时停止任务，保留未完成结果，不再调用模型。"},
          downstream_effect:{en:"Durable evidence remains available for a later continuation.",zh:"持久证据仍可用于后续继续处理。"},
          risks:{en:"The requested task is not delivered in this run.",zh:"本次执行未交付所请求的完整任务。"},
          reversibility:{en:"A future task can continue from the saved state.",zh:"后续任务可从保存的状态继续。"},
        },
      }),
    };
  }
  if (source.display_template === "engineering_amendment" && source.display_context) {
    return {...source,
      question:String(source.display_context.reason || "") + "\n" + t({en:"Exact changes (before / after):",zh:"具体变更（before 为原要求，after 为新要求）："})
        + "\n" + JSON.stringify(source.display_context.changes, null, 2) + "\n" + t({en:"Adopt this adjustment?",zh:"是否采用此调整？"}),
      why_user_must_decide:t({en:"Existing engineering requirements would change; missing evidence cannot be silently waived.",zh:"现有工程要求将被修改，不能静默免除缺失证据。"}),
      options:options({
        accept_engineering_amendment:{
          label:{en:"Adopt adjustment",zh:"采用调整"},principle:{en:"Explicitly amend engineering requirements and preserve original facts.",zh:"明确修改工程要求，保留原始事实。"},
          immediate_effect:{en:"Adopt the displayed step changes.",zh:"采用当前展示的步骤调整。"},
          downstream_effect:{en:"Runtime checks the new dependencies and evidence; old evidence is not marked as passed.",zh:"运行时按新的依赖与证据检查；旧证据不会被标为通过。"},
          risks:{en:"Removed checks no longer provide their previous assurance.",zh:"移除的检查不再保证交付质量。"},reversibility:{en:"You can explicitly request another adjustment.",zh:"可以明确申请再次调整。"},
        },
        keep_engineering_workflow:{
          label:{en:"Keep requirements",zh:"保留原要求"},principle:{en:"Continue satisfying existing engineering requirements.",zh:"继续满足原来的工程条件。"},
          immediate_effect:{en:"Leave the current workflow unchanged.",zh:"不修改当前工作流。"},downstream_effect:{en:"Existing missing evidence is still required.",zh:"仍需补齐现有缺失证据。"},
          risks:{en:"This may require more time or verification unavailable in the current environment.",zh:"可能需要额外时间或当前环境无法提供的验证。"},reversibility:{en:"You can propose a new adjustment later.",zh:"稍后仍可提出新调整。"},
        },
      }),
    };
  }
  return source;
}
