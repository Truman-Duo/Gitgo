import type {Copy} from "./pageContract.js";
import {chordLabel as key} from "../input/bindings.js";
import {menuHelpGuide,type MenuHelpTopic} from "./menuHelp.js";
export type HelpTopic="providers"|"model"|"effort"|"interrupt"|MenuHelpTopic|`publish${string}`;
type Translate=(value:Copy)=>string;
export function moduleHelpGuide(topic:HelpTopic,t:Translate){
 const menu=menuHelpGuide(topic,t);if(menu)return menu;
 if(topic==="interrupt")return {title:t({en:"Interruption",zh:"中断确认"}),purpose:t({en:"Choose whether to stop the displayed process or keep it working.",zh:"决定停止当前显示的进程，或让它继续工作。"}),steps:[
  t({en:`${key("leftRight")} selects an action, ${key("enter")} confirms and ${key("escape")} closes without interrupting. The initial selection keeps the process working.`,zh:`${key("leftRight")} 选择动作，${key("enter")} 确认，${key("escape")} 关闭且不中断；初始选择为继续工作。`}),
  t({en:"Confirming interruption asks the existing runtime to stop the displayed target and its indicated descendants. Opening or closing this view does not stop a request. Conversation and completed file changes are preserved; no automatic rollback occurs.",zh:"确认中断后，沿现有运行时停止显示的目标及其标明的子进程。仅打开或关闭此处不会停止请求；保留对话与已完成文件更改，不自动回滚。"}),
  t({en:"The acknowledgement distinguishes a stopped tree from a stop request that is still being processed. Failure remains visible and does not become a successful interruption.",zh:"回执区分已经停止与正在处理中断请求；失败会显示原因，不会冒充中断成功。"}),
 ]};
 const model=topic==="model",effort=topic==="effort";
 if(model||effort)return {title:t(model?{en:"Model selection",zh:"切换模型"}:{en:"Reasoning effort",zh:"思考强度"}),purpose:t(model?{en:"Choose a configured model of the current API.",zh:"切换当前 API 已配置的模型。"}:{en:"Adjust the current model's reasoning effort.",zh:"调整当前模型的思考强度。"}),steps:model?[
  t({en:`Type to filter models, ${key("upDown")} selects and ${key("enter")} confirms. The current tag identifies the saved selection.`,zh:`输入文字筛选模型，${key("upDown")} 选择，${key("enter")} 确认。“当前”表示已保存的选择。`}),
  t({en:"Only models saved under the active API appear here. Add or edit them in /config providers. Connection protocols are edited in the API configuration.",zh:"这里只显示当前 API 保存的模型；在 /config providers 中添加或编辑。连接协议在 API 配置里管理。"}),
  t({en:"Changing the model requires an idle task boundary. A busy refusal retains the previous selection and does not interrupt the running request.",zh:"切换模型需要任务处于空闲边界；忙碌时保留原选择，不中断运行中的请求。"}),
  t({en:`${key("escape")} cancels without saving. /help opens this guide; closing it restores your filter and selection.`,zh:`${key("escape")} 取消且不保存；/help 打开本说明，关闭后恢复筛选与选择。`}),
 ]:[
  t({en:`${key("leftRight")} adjusts the highlighted level; ${key("enter")} saves it and ${key("escape")} cancels.`,zh:`${key("leftRight")} 调整档位，${key("enter")} 保存，${key("escape")} 取消。`}),
  t({en:"The saved value takes precedence, followed by a declared service default. Otherwise Gitgo initially selects high, medium, or the first available level, in that order. Only a confirmed selection is saved.",zh:"已有设置优先，其次服务默认值；均缺失时依次初选 high、medium 或首个可用档位。只有确认后才保存。"}),
  t({en:"Only declared levels are offered. The endpoints mean faster and deeper reasoning. An unavailable scale is not proof that arbitrary effort parameters are accepted.",zh:"仅提供已声明的档位，两端分别表示更快和更深入。未提供刻度时，不能据此认定服务接受任意强度参数。"}),
  t({en:"The setting persists after restart. An in-flight request keeps its parameters; the saved effort applies from the next model request without stopping the daemon.",zh:"设置在重启后保留；已发出的请求继续使用原参数，下一次模型请求读取保存值，不停止 daemon。"}),
 ]};
 const section=topic.slice("publish.".length);
 const sections:Record<string,{title:Copy;purpose:Copy;steps:Copy[]}>= {
  pr:{title:{en:"PR details",zh:"PR 详情"},purpose:{en:"Inspect one recorded PR before commenting, reviewing or handling it.",zh:"查看指定 PR 的已记录内容，再评论、审查或处理。"},steps:[
   {en:`${key("upDown")} selects View changes, Discussion and records, or Handle this PR; ${key("enter")} opens the selected action and ${key("escape")} returns one level.`,zh:`${key("upDown")} 选择查看变更、讨论与记录或处理这个 PR，${key("enter")} 打开，${key("escape")} 逐级返回。`},
   {en:"View changes opens the platform's saved file patches. In a diff, C prepares a review for that file. Missing or truncated patches remain explicitly unverified. A list observation must be refreshed to a full PR observation before drafting a review.",zh:"查看变更打开平台保存的文件差异；在差异中按 C 准备该文件的审查。缺失或截断的差异仍属未验证；列表观察需刷新为完整 PR 观察后才能起草审查。"},
   {en:"Discussion opens comments, replies and recorded reviews. Handle this PR opens checks, merge/takeover, requested changes, deferral or a close draft. Preparing a comment or close draft does not send it; sending requires the explicit existing action.",zh:"讨论包含评论、回复及已保存的审查。处理包括核对检查、合入或接手、要求修改、暂缓及关闭草稿。准备评论或关闭草稿不会发送，仍需显式执行既有发送操作。"},
   {en:"Browsing never creates a candidate. Integration or takeover needs a candidate bound to the exact PR source; unrelated local Git is not substituted for its platform patch.",zh:"浏览不创建候选；合入或接手需要绑定到该 PR 确切来源的候选，不用无关本地 Git 冒充平台差异。"},
  ]},
  history:{title:{en:"Git history",zh:"Git 历史"},purpose:{en:"Inspect commits, parent relationships and their fixed differences.",zh:"查看提交、父子关系与固定差异。"},steps:[
   {en:`${key("leftRight")} selects commits, ${key("enter")} opens their diff and ${key("escape")} returns. Newer commits are on the left; graph edges are actual Git parents.`,zh:`${key("leftRight")} 选择提交，${key("enter")} 查看差异，${key("escape")} 返回；左新右旧，图中连线为真实 Git 父关系。`},
   {en:"Filled markers select a commit; the highlighted ring identifies HEAD. The text compatibility view uses O for selection, * for commits and > for continuation. /more reads the next history page, /parent N selects a merge parent and /refresh reloads observation.",zh:"实心表示选中，光圈表示 HEAD；文字兼容视图使用 O 表示选中、* 表示提交、> 表示延续。/more 读取下一页历史，/parent N 选择合并父提交，/refresh 刷新观察。"},
  ]},
  flow:{title:{en:"Role flow",zh:"三角色流转"},purpose:{en:"Inspect the saved plan and its actual processing steps.",zh:"查看已保存计划及实际处理步骤。"},steps:[
   {en:`${key("upDown")} selects steps or actions and ${key("enter")} opens them. Workspace, Trial and Formal are repository roles; formal commits may remain in a local Workspace branch.`,zh:`${key("upDown")} 选择步骤或动作，${key("enter")} 打开。Workspace、Trial、Formal 表示仓库角色，正式提交也可以保存在本地 Workspace 分支。`},
   {en:"Flow edges describe the saved plan, not completed transfers. Inspect source refs, target refs, object IDs and step receipts. Only currently permitted actions can execute.",zh:"流转连线描述已保存计划，不表示传输已完成；核对来源、目标、对象 ID 与步骤回执，只能执行当前允许的动作。"},
  ]},
  task:{title:{en:"Linked task",zh:"关联任务"},purpose:{en:"Read the conversation of the exact linked task.",zh:"读取确切关联任务的对话。"},steps:[
   {en:`${key("upDown")} scrolls, D opens details, P switches to a recorded parent task and ${key("escape")} returns to Publish.`,zh:`${key("upDown")} 滚动，D 打开详情，P 切换到已记录的父任务，${key("escape")} 返回 Publish。`},
   {en:"Task and process identities are checked before displaying a conversation. A replaced or unavailable task is not substituted with another conversation.",zh:"显示对话前核对任务与进程身份；任务被替换或不可用时，不显示其他任务冒充。"},
  ]},
  preview:{title:{en:"Prepared changes",zh:"已准备的变更"},purpose:{en:"Inspect the prepared content tree and its diff.",zh:"查看已准备的内容树与差异。"},steps:[
   {en:`${key("upDown")} or the wheel browses the diff; ${key("escape")} returns. Source and result object IDs identify the exact content being reviewed.`,zh:`${key("upDown")} 或滚轮浏览差异，${key("escape")} 返回；来源和结果对象 ID 标明本次审阅的确切内容。`},
   {en:"Reading a preview does not execute it. A truncation warning means some changes are omitted from this view; those changes are not implicitly approved.",zh:"查看预览不会执行操作；出现截断提示时，表示此处省略了部分变更，不能视作已批准。"},
  ]},
 };
 if(sections[section]){const own=sections[section]!;return {title:t(own.title),purpose:t(own.purpose),steps:[...own.steps.map(t),t({en:"/help preserves this view. Closing help restores its selection and scroll position without executing an action.",zh:"/help 保留当前页面；关闭后恢复选择与滚动位置，不会执行操作。"})]};}
 return {title:t({en:"Publish",zh:"Publish 使用说明"}),purpose:t({en:"Organize local work, collaborate, release versions and configure publishing.",zh:"整理本地工作、参与协作、发布版本及配置发布行为。"}),steps:[
  t({en:`${key("leftRight")} changes headers or the focused graph node; ${key("upDown")} selects a row; ${key("enter")} opens it; ${key("escape")} returns one level. Space marks source commits.`,zh:`${key("leftRight")} 切换页头或当前图节点，${key("upDown")} 选择行，${key("enter")} 打开，${key("escape")} 逐级返回；Space 多选来源提交。`}),
  t({en:"Workspace shows process Git above formal Git. Select a source range, review its semantic intent and prepare a formal commit. Provenance links show which rounds contributed; they are not Git parent edges.",zh:"Workspace 上方是过程 Git，下方是正式 Git。选择来源范围，核对语义后准备正式提交；来源对应线表示哪些回合组成提交，与 Git 父子连线不同。"}),
  t({en:"Collaboration includes your outgoing contributions and incoming PRs. Select a graph node or record to inspect changes, discussion and review, then choose merge or takeover.",zh:"协作包含自己的提交及收到的 PR；选择图节点或记录查看变更、讨论和审查，再选择合入或接手。"}),
  t({en:"Release preparation, artifacts, tags and platform releases have separate recorded states. A prepared plan or graph connection does not mean publication completed; inspect actual receipts.",zh:"发布准备、产物、标签和平台发布分别记录状态。已准备计划或图中的连线不表示发布完成，以实际回执为准。"}),
  t({en:`Repository, account, commit conventions, privacy/authorship and verification settings use the same form interaction: ${key("enter")} edits or stages one field, ${key("formSave")} saves the form, ${key("escape")} cancels one level.`,zh:`仓库、账户、提交规范、隐私署名和验证设置统一使用表单交互：${key("enter")} 编辑或暂存当前字段，${key("formSave")} 保存整张表单，${key("escape")} 逐级取消。`}),
  t({en:"Check entries use file.py | seeds | seconds, separated by semicolons. Every configured check is required. Privacy and authorship save together; changing a target or policy invalidates stale prepared evidence.",zh:"检查项使用 file.py | 随机种子 | 秒数，以分号分隔；配置的检查都必须通过。隐私与署名一起保存；更换目标或策略后，过期的预备证据需重新核对。"}),
  t({en:"Use / to discover the actions currently available. /prepare, /formal and /combine prepare candidates; /verify runs checks; /execute performs the reviewed action; /run verify execute requests consecutive steps. Actions still follow actual permissions and saved revisions.",zh:"输入 / 查看当前可用操作。/prepare、/formal、/combine 准备候选，/verify 运行检查，/execute 执行已审阅操作，/run verify execute 请求连续步骤；仍按实际权限和保存版本执行。"}),
  t({en:"/remote reads platform objects. /comment, /review, /reply, /pull and /issue prepare drafts; /send sends an explicit draft. /history and /preview inspect changes; /flow shows role transfers and /task opens linked work.",zh:"/remote 读取平台对象；/comment、/review、/reply、/pull、/issue 准备草稿，/send 发送指定草稿。/history、/preview 查看变更，/flow 查看角色流转，/task 打开关联任务。"}),
  t({en:"An uncertain external result must be reconciled before repeating the write. /reconcile, /platform-reconcile, /distribution-reconcile and /branch-reconcile read the corresponding saved effect. /pause stops further steps.",zh:"外部结果不确定时，先核对再决定是否重做。/reconcile、/platform-reconcile、/distribution-reconcile、/branch-reconcile 分别核对已保存效果；/pause 停止后续步骤。"}),
  t({en:"/help is available outside text editing. It preserves drafts, graph selections and scroll positions. While editing text or JSON, /help is literal content; leave field editing before invoking help.",zh:"退出文本编辑后输入 /help，保留草稿、图节点选择和滚动位置；文本或 JSON 编辑中的 /help 是字面内容，先退出字段编辑再查看帮助。"}),
 ]};
}
export function moduleHelpKeys(topic:HelpTopic,t:Translate){return [
 {keys:key(topic==="effort"||topic==="publish.history"?"leftRight":"upDown"),meaning:t({en:"Select / adjust",zh:"选择／调整"})},
 {keys:key("enter"),meaning:t({en:"Open, stage or confirm the focused action",zh:"打开、暂存或确认当前操作"})},
 ...(topic.startsWith("publish")||topic==="providers"||topic==="create"||topic==="export"?[{keys:key("formSave"),meaning:t({en:"Save the complete form",zh:"保存整张表单"})},{keys:key("leftRight"),meaning:t({en:"Change a header, graph node or enum value",zh:"切换页头、图节点或枚举值"})}]:[]),
 ...(topic==="providers"?[{keys:key("providerModelsRefresh"),meaning:t({en:"Refresh models and connection checks on a model row",zh:"在模型行刷新模型及连接检查"})},{keys:key("delete"),meaning:t({en:"Remove the focused model from the draft",zh:"从草稿移除当前模型"})}]:[]),
 {keys:key("escape"),meaning:t({en:"Cancel the current edit or return one level",zh:"取消当前修改或逐级返回"})},
 ];}
