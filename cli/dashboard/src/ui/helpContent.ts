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
export function helpGuide(scene:Scene,t:Translate){
 const translate=t;
 t=value=>translate({en:keyHintText(value.en),zh:keyHintText(value.zh),keysResolved:true});
 const title=t(sceneTitles[scene]);
 if(scene==="projects")return {title,purpose:t({en:"Open a project workspace, create a project, or manage application settings and updates.",zh:"进入项目继续工作，创建项目，或管理软件设置与更新。"}),steps:[
  t({en:"Select a project with the arrow keys and press Enter to open its main conversation.",zh:"用上下方向键选中项目，按 Enter 进入主进程对话。",keys:true}),
  t({en:`Use /create to create or register a workspace. Enter edits/confirms one field; ${key("formSave")} saves the complete form. Esc undoes the current edit, then cancels the form.`,zh:`用 /create 创建或登记工作区；Enter 进入修改／暂存当前字段，${key("formSave")} 保存整张表单；Esc 撤销当前修改，退出修改后再按一次取消表单。`}),
  t({en:"Use /stats for daily usage across projects, /config for settings, and /update for software updates. /config keybindings shows, tests and edits keyboard bindings. /bin manages archived projects.",zh:"/stats 查看跨项目的每日用量，/config 修改设置，/config keybindings 查看或切换绑键，/update 管理软件更新，/bin 管理已归档项目。"}),
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
  t({en:"Use /processlist to inspect subprocesses. In the main conversation, Tab focuses the working/waiting subprocess strip when present; arrows select and Enter opens it.",zh:"用 /processlist 查看子进程；主对话底部有工作中或等待中的子进程时，Tab 聚焦该区域，左右选择，Enter 打开。",keys:true}),
  t({en:"Use /btw followed by a question for a read-only side discussion. Scroll back to inspect the main conversation; applying a change still requires confirmation.",zh:"输入 /btw 加问题，发起只读旁路讨论；可以上滚回看主对话，需要应用变更时仍须确认。"}),
  t({en:"/runtime shows lessons, contracts, governance, memory, context and custom actions. /stats shows recorded task turns and timing. /publish manages trial and formal changes.",zh:"/runtime 查看经验、契约、治理、记忆、上下文和自定义动作；/stats 查看已记录的任务轮次与耗时；/publish 管理试验与正式变更。"}),
  t({en:"Esc during execution opens the interrupt confirmation. Read the target before confirming; /undo separately previews rewinding the latest conversation turn.",zh:"执行中按 Esc 打开中断确认，确认前核对目标；/undo 则单独预览撤回最近一轮会话。",keys:true}),
 ]};
}
export function helpKeys(scene:Scene,t:Translate){
 const chat=scene==="workspace"||scene==="agent_detail";
 return [...BINDING_DEFINITIONS].sort((a,b)=>Number(b.id==="command.open")-Number(a.id==="command.open")).filter(item=>item.contexts.includes("*") && (chat || !item.id.startsWith("editor."))
   || item.id==="command.open" || item.contexts.includes("form")
   || chat && (item.contexts.includes("decision") || item.contexts.includes("task") || item.contexts.includes("btw")))
  .map(item=>({keys:applicationBindings.displayLabel(item.id),meaning:t(item.label)+" · "+t(item.condition)}));
}
