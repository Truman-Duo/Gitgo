import {keyHintText} from "../input/hints.js";
import type {Scene} from "../state/store.js";
import type {Copy} from "./pageContract.js";
import {chordLabel as key} from "../input/bindings.js";
import {applicationBindings, BINDING_DEFINITIONS} from "../input/router.js";
type Translate=(copy:Copy)=>string;
const sceneTitles:Record<Scene,Copy>={
 projects:{en:"Project list",zh:"项目列表"},
 workspace:{en:"Main conversation",zh:"主进程对话"},
 process_list:{en:"Subprocess list",zh:"子进程列表"},
 agent_detail:{en:"Subprocess conversation",zh:"子进程对话"},
};
export function providerHelpGuide(t:Translate){
 return {title:t({en:"Provider configuration",zh:"Provider 配置"}),purpose:t({en:"In the Provider editor under /config providers, choose a provider template, then configure its API connection and models.",zh:"在 /config providers 的 Provider 编辑器里，先选服务商模板，再配置 API 连接和模型。"}),steps:[
  t({en:"A preset fills the connection name, address and protocol. It does not mean the connection is saved, verified or selected as the main provider.",zh:"预设用于填写连接名称、地址和协议，不代表连接已经保存、验证通过或设为主模型。"}),
  t({en:"Model, effort and interruption controls open between the input border and the original status bar. Model choices use the command suggestion layout with neutral gray/white selection, without protocols or position counters. Preset choices show names without addresses or counters. Backup connections are ordered by their indented rows; when automatic failover is off, they are gray but remain editable.",zh:"模型选择、思考强度和中断确认位于文本框下边线与原状态栏之间。模型选择沿用命令推荐的布局，选中使用灰白色，隐藏协议与序号计数；预设只显示名称，不展示地址或计数。备用连接按缩进各行的先后排序，关闭自动备用切换后变灰，仍可编辑。"}),
  t({en:"The Providers tab directly shows the connections. Its top switch enables automatic failover; each API has its own two indented backup slots. Enter edits a slot, left/right chooses another API or none, and Enter saves. Switching the current API uses that API's own queue, without following backups recursively.",zh:"模型服务商页直接显示连接；顶部总开关控制自动备用，每个 API 下缩进展示两个自己的备用位。Enter 编辑备用位，左右选择其他 API 或未配置，再 Enter 保存。切换主 API 使用它自己的队列，不递归追踪备用的备用。"}),
  t({en:"Opening the API configuration starts discovery immediately. The address and protocol identify the catalogue; a key enables the account-specific list after Enter confirms the field. A name and a saved connection are not required for model selection. Manual model IDs remain available if discovery fails. Models are configured in one vertical collection, without selecting the runtime model here.",zh:"进入 API 填写页立即拉取模型；地址和协议用于获取候选，密钥字段按 Enter 暂存后核对账号列表。选择模型不要求先填名称或保存连接，获取失败仍可手填型号。模型只用一份竖排集合管理，不在这里选择运行模型。"}),
  t({en:`Enter stages the current field; ${key("formSave")} submits the entire API connection, not just that field. Missing connection details are identified with the draft retained. Once the connection is filled, saving with no configured models opens automatic model discovery: wait for candidates, select models, or use manual IDs if discovery fails. Save the selection into the draft, then save the entire API. Only selected models are persisted.`,zh:`Enter 暂存当前字段；${key("formSave")} 提交整份 API 连接，不是只保存当前字段。连接信息缺项时保留草稿并定位缺项；连接已填好但尚无模型时，保存会直接进入自动获取的候选页，等待结果后勾选，获取失败可手填。保存选择合入草稿后，再保存整份 API；只保存选定的模型。`}),
  t({en:"Background discovery uses staged connection fields. Editing the address, key or protocol cancels the old preview; success and failure from an old connection are ignored. Without a key, unavailable public reference data is a waiting state. A model-list error appears only as a model notice, keeps manual IDs available and is not a field or save failure.",zh:"后台发现只读取已暂存的连接信息；编辑地址、密钥或协议会取消旧预览，旧连接的成功与失败都不再显示。无密钥且公开参考不可用时显示待填写状态；列表失败只作为模型提示，不算字段或保存失败，仍可手填型号。"}),
  t({en:`Add models opens a searchable batch selector. Use Enter to toggle candidates and ${key("formSave")} to save this selection, or edit manual IDs directly on the same page (one per line). Only selected models enter the draft. ${key("providerModelsRefresh")} refreshes discovery and connection checks on a model or Add models row.`,zh:`“添加模型”打开可搜索的批量选择页，Enter 连续勾选候选，${key("formSave")} 保存本次选择，或在同页的手填字段直接粘贴每行一个型号。只有选定的型号进入草稿。在模型行或添加入口按 ${key("providerModelsRefresh")} 刷新候选并重新检查连接。`}),
  t({en:`Advanced opens the complete editable configuration JSON. Context/output budgets, effort and request parameters live there. ${key("formSave")} validates and saves the entire API; success returns one level to the API form; errors retain the document, and Esc cancels this JSON edit. Credentials remain in the API Key field.`,zh:`“高级”打开完整可编辑的配置 JSON；上下文、输出预算、思考强度和请求参数都在其中管理。${key("formSave")} 校验并保存整份 API；成功只返回上一层 API 配置页，出错保留原文，Esc 取消本次 JSON 修改。密钥由专门字段管理。`}),
  t({en:"Dots show the last connection check: gray unknown/reference, orange checking, green a real model response, red failure. Configured models are checked sequentially without selecting a row or pressing Enter, once per stable connection/model/parameter combination in this editor. Unselected catalogue candidates are not checked. Editing parameters or refreshing permits a new check. Checks make real API requests; they do not verify tool or streaming capability.",zh:"状态点表示最近连接检查：灰色为未知／参考，橙色为检查中，绿色为已收到真实模型响应，红色为失败。已配置模型自动逐个检查，无需选中后按 Enter；同一编辑器内每个稳定的连接、模型与参数组合只自动尝试一次，不检查未选用的候选目录。修改参数或刷新可重新检查。检查会发起真实 API 请求，不代表工具调用或流式能力已验证。"}),
  t({en:`Type /help outside field editing to open this guide. ${key("formSave")} saves every model profile together; background checks do not prevent saving. ${key("escape")} returns from help with the draft retained.`,zh:`退出字段编辑后输入 /help 查看说明。${key("formSave")} 一次保存全部模型配置，后台连接检查不会阻止保存；${key("escape")} 关闭帮助并保留草稿。`}),
  t({en:"/model and /effort are available in the project list and main conversation. Both open in the footer and keep the main content visible. /model searches only the current API's configured models and requires an idle task boundary. /effort shows a wider scale with only its triangle accented. A saved effort or declared default takes precedence; otherwise the initial selection is high, then medium, then the first available level. Enter persists the selection, Esc cancels without saving. Existing requests finish unchanged; managed clients read the saved effort from their next model request, including after restart.",zh:"/model、/effort 仅在项目列表和具体项目主对话提供，均在底部展开并保留正文。/model 只搜索当前 API 已配置的模型，切换仍需等待任务空闲。/effort 使用加宽刻度，仅三角着色；已有设置或服务默认值优先，否则初始选中 high、其次 medium、最后首个可用档位。Enter 持久保存，Esc 取消且不写入；已发出的请求保持原参数，运行客户端从下一次模型请求读取保存值，重启后仍保留。"}),
  t({en:"For a new remote API, Esc from a field first undoes its edit; Esc from the API form returns to Choose Provider, and Esc there returns to the Providers tab. Choosing the same provider again retains the draft. Model selection, help and JSON each return to the API form. An existing API edited directly returns to its actual caller.",zh:"新建远端 API 时，字段内 Esc 先撤销编辑；API 配置页 Esc 返回选择 Provider，再 Esc 回到模型服务商页。重新选择同一服务商保留草稿；模型选择、帮助和 JSON 各自返回 API 配置页。直接编辑已有 API 时返回实际入口。"}),
 ]};
}
export function helpGuide(scene:Scene,t:Translate){
 const translate=t;
 t=value=>translate({en:keyHintText(value.en),zh:keyHintText(value.zh),keysResolved:true});
 const title=t(sceneTitles[scene]);
 if(scene==="projects")return {title,purpose:t({en:"Open a project workspace, create a project, or manage application settings and updates.",zh:"进入项目继续工作，创建项目，或管理软件设置与更新。"}),steps:[
  t({en:"Select a project with the arrow keys and press Enter to open its main conversation.",zh:"用上下方向键选中项目，按 Enter 进入主进程对话。",keys:true}),
  t({en:`Use /create to create or register a workspace. Enter edits/confirms one field; ${key("formSave")} saves the complete form. Esc undoes the current edit, then cancels the form.`,zh:`用 /create 创建或登记工作区；Enter 进入修改／暂存当前字段，${key("formSave")} 保存整张表单；Esc 撤销当前修改，退出修改后再按一次取消表单。`}),
  t({en:"Use /stats for daily usage across projects, /config for settings, and /update for software updates. /config keybindings shows and edits keyboard bindings. /bin manages archived projects.",zh:"/stats 查看跨项目的每日用量，/config 修改设置，/config keybindings 查看或切换绑键，/update 管理软件更新，/bin 管理已归档项目。"}),
  t({en:"Type / to browse commands, select one, then press Enter. Commands requiring text fill the editor before submission.",zh:"输入 / 浏览命令，选中后按 Enter；需要补充文字的命令会先填入输入框，补全后再提交。",keys:true}),
 ]};
 if(scene==="process_list")return {title,purpose:t({en:"Inspect subprocess status and relationships, and open a subprocess conversation.",zh:"查看子进程状态与相互关系，并进入具体子进程继续交流。"}),steps:[
  t({en:"Select a row with the arrow keys. Indentation shows parentage; dependency labels identify prerequisite subprocesses.",zh:"用上下方向键选中一项；缩进表示父子关系，依赖标记表示需要先完成的子进程。"}),
  t({en:"Press Enter to open the selected subprocess. Tab or Esc returns to the main conversation.",zh:"按 Enter 进入选中的子进程；Tab 或 Esc 返回主进程对话。",keys:true}),
  t({en:"Use /create followed by a task description to ask the main process to create and supervise a subprocess.",zh:"输入 /create 加任务说明，请主进程创建并监督子进程。"}),
  t({en:"Use /detail to inspect the selected subprocess model, budget, workspace and relationships. Esc returns to this list.",zh:"选中子进程后输入 /detail，查看模型、预算、工作目录与关系；Esc 返回当前列表。",keys:true}),
  t({en:"/rename changes the selected subprocess name; /archive archives its list entry without stopping its execution.",zh:"/rename 修改所选子进程名称；/archive 归档列表项，不会停止其执行。"}),
 ]};
 return {title,purpose:scene==="workspace"
  ?t({en:"Describe your goal to the main process, follow its progress, and answer questions or permission requests.",zh:"向主进程描述目标，查看执行进度，并回答需求澄清或权限请求。"})
  :t({en:"Talk directly with this subprocess and inspect its conversation. Permission and question cards remain explicit user decisions.",zh:"直接与当前子进程交流并查看其对话；需求与权限问题仍通过卡片由用户明确决定。"}),steps:[
  t({en:"Type a goal or follow-up and press Enter to send it. Shift+Enter inserts a new line; Ctrl+G opens the complete draft in an external editor.",zh:"输入目标或补充说明，按 Enter 发送；Shift+Enter 换行，Ctrl+G 在外部编辑器中编辑完整草稿。",keys:true}),
  ...(scene==="agent_detail"?[t({en:"Use /detail for this subprocess model, budget, workspace and relationships. Messages are delivered through its safe-boundary mailbox; actual delivery status remains in the conversation.",zh:"/detail 查看当前子进程的模型、预算、工作目录与关系。输入通过该子进程的安全边界消息通道投递，实际投递状态仍显示在对话中。"})]:[]),
  t({en:"Review a question card before answering. You may discuss it in free text without first accepting a proposed option.",zh:"遇到问题卡片先核对内容；不接受现有选项时，可以直接用文字讨论，不必先选定一个方案。"}),
  t({en:`Question cards: ${key("upDown")} selects options, ${key("enter")} chooses or edits a reply and ${key("escape")} returns from editing. The previous-question arrow revisits an earlier question. Permission and adoption targets must be explicitly confirmed.`,zh:`问题卡片：${key("upDown")} 选择选项，${key("enter")} 确认或编辑答复，${key("escape")} 退出编辑；上一题箭头回看前题。授权及采用目标必须明确确认。`}),
  t({en:"Use /processlist to inspect subprocesses. In the main conversation, Tab focuses the working/waiting subprocess strip when present; arrows select and Enter opens it.",zh:"用 /processlist 查看子进程；主对话底部有工作中或等待中的子进程时，Tab 聚焦该区域，左右选择，Enter 打开。",keys:true}),
  t({en:"Use /btw followed by a question for a read-only side discussion. Scroll back to inspect the main conversation; applying a change still requires confirmation.",zh:"输入 /btw 加问题，发起只读旁路讨论；可以上滚回看主对话，需要应用变更时仍须确认。"}),
  t({en:"/runtime shows lessons, contracts, governance, memory, context and custom actions. /stats shows recorded task turns and timing. /publish manages trial and formal changes.",zh:"/runtime 查看经验、契约、治理、记忆、上下文和自定义动作；/stats 查看已记录的任务轮次与耗时；/publish 管理试验与正式变更。"}),
  t({en:"Esc during execution opens the interrupt confirmation. Read the target before confirming; /undo separately confirms rewinding the latest conversation turn.",zh:"执行中按 Esc 打开中断确认，确认前核对目标；/undo 则单独确认撤回最近一轮会话。",keys:true}),
 ]};
}
export function helpKeys(scene:Scene,t:Translate){
 const chat=scene==="workspace"||scene==="agent_detail";
 return [...BINDING_DEFINITIONS].sort((a,b)=>Number(b.id==="command.open")-Number(a.id==="command.open")).filter(item=>item.contexts.includes("*") && (chat || !item.id.startsWith("editor."))
   || item.id==="command.open" || item.contexts.includes("form")
   || chat && (item.contexts.includes("decision") || item.contexts.includes("task") || item.contexts.includes("btw")))
  .map(item=>({keys:applicationBindings.displayLabel(item.id),meaning:t(item.label)+" · "+t(item.condition)}));
}
