import type {Scene} from "../state/store.js";
import type {Copy} from "./pageContract.js";
import {chordLabel as key} from "../input/bindings.js";
import {applicationBindings} from "../input/router.js";
type Translate=(copy:Copy)=>string;
const sceneTitles:Record<Scene,Copy>={
 projects:{en:"Project list",zh:"项目列表"},
 workspace:{en:"Main conversation",zh:"主进程对话"},
 process_list:{en:"Subprocess list",zh:"子进程列表"},
 agent_detail:{en:"Subprocess conversation",zh:"子进程对话"},
};
export function helpGuide(scene:Scene,t:Translate){
 const translate=t;
 const chords:Record<string,string>={"Ctrl+G":"ctrlG","Shift+Enter":"shiftEnter",Enter:"enter",Tab:"tab",Esc:"escape"};
 t=value=>translate(value).replace(/Ctrl\+G|Shift\+Enter|Enter|Tab|Esc/g,match=>key(chords[match]!));
 const title=t(sceneTitles[scene]);
 if(scene==="projects")return {title,purpose:t({en:"Open a project workspace, create a project, or manage application settings and updates.",zh:"进入项目继续工作，创建项目，或管理软件设置与更新。"}),steps:[
  t({en:"Select a project with the arrow keys and press Enter to open its main conversation.",zh:"用上下方向键选中项目，按 Enter 进入主进程对话。"}),
  t({en:"Use /create to create or register a workspace. Check the project name and directory before confirming.",zh:"用 /create 创建或登记工作区；确认前核对项目名称和目录。"}),
  t({en:"Use /stats for daily usage across projects, /config for settings, and /update for software updates. /bin manages archived projects.",zh:"/stats 查看跨项目的每日用量，/config 修改设置，/update 管理软件更新，/bin 管理已归档项目。"}),
  t({en:"Type / to browse commands, select one, then press Enter. Commands requiring text fill the editor before submission.",zh:"输入 / 浏览命令，选中后按 Enter；需要补充文字的命令会先填入输入框，补全后再提交。"}),
 ]};
 if(scene==="process_list")return {title,purpose:t({en:"Inspect subprocess status and relationships, and open a subprocess conversation.",zh:"查看子进程状态与相互关系，并进入具体子进程继续交流。"}),steps:[
  t({en:"Select a row with the arrow keys. Indentation shows parentage; dependency labels identify prerequisite subprocesses.",zh:"用上下方向键选中一项；缩进表示父子关系，依赖标记表示需要先完成的子进程。"}),
  t({en:"Press Enter to open the selected subprocess. Tab or Esc returns to the main conversation.",zh:"按 Enter 进入选中的子进程；Tab 或 Esc 返回主进程对话。"}),
  t({en:"Use /create followed by a task description to ask the main process to create and supervise a subprocess.",zh:"输入 /create 加任务说明，请主进程创建并监督子进程。"}),
  t({en:"Use /detail to inspect the selected subprocess model, budget, workspace and relationships. Esc returns to this list.",zh:"选中子进程后输入 /detail，查看模型、预算、工作目录与关系；Esc 返回当前列表。"}),
  t({en:"/rename changes the selected subprocess name; /archive archives its list entry without stopping its execution.",zh:"/rename 修改所选子进程名称；/archive 归档列表项，不会停止其执行。"}),
 ]};
 return {title,purpose:scene==="workspace"
  ?t({en:"Describe your goal to the main process, follow its progress, and answer questions or permission requests.",zh:"向主进程描述目标，查看执行进度，并回答需求澄清或权限请求。"})
  :t({en:"Talk directly with this subprocess and inspect its conversation. Permission and question cards remain explicit user decisions.",zh:"直接与当前子进程交流并查看其对话；需求与权限问题仍通过卡片由用户明确决定。"}),steps:[
  t({en:"Type a goal or follow-up and press Enter to send it. Shift+Enter inserts a new line; Ctrl+G opens the complete draft in an external editor.",zh:"输入目标或补充说明，按 Enter 发送；Shift+Enter 换行，Ctrl+G 在外部编辑器中编辑完整草稿。"}),
  ...(scene==="agent_detail"?[t({en:"Use /detail for this subprocess model, budget, workspace and relationships. Messages are delivered through its safe-boundary mailbox; actual delivery status remains in the conversation.",zh:"/detail 查看当前子进程的模型、预算、工作目录与关系。输入通过该子进程的安全边界消息通道投递，实际投递状态仍显示在对话中。"})]:[]),
  t({en:"Review a question card before answering. You may discuss it in free text without first accepting a proposed option.",zh:"遇到问题卡片先核对内容；不接受现有选项时，可以直接用文字讨论，不必先选定一个方案。"}),
  t({en:"Use /processlist to inspect subprocesses. In the main conversation, Tab focuses the working/waiting subprocess strip when present; arrows select and Enter opens it.",zh:"用 /processlist 查看子进程；主对话底部有工作中或等待中的子进程时，Tab 聚焦该区域，左右选择，Enter 打开。"}),
  t({en:"Use /btw followed by a question for a read-only side discussion. Scroll back to inspect the main conversation; applying a change still requires confirmation.",zh:"输入 /btw 加问题，发起只读旁路讨论；可以上滚回看主对话，需要应用变更时仍须确认。"}),
  t({en:"/runtime shows lessons, contracts, governance, memory, context and custom actions. /stats shows recorded task turns and timing. /publish manages trial and formal changes.",zh:"/runtime 查看经验、契约、治理、记忆、上下文和自定义动作；/stats 查看已记录的任务轮次与耗时；/publish 管理试验与正式变更。"}),
  t({en:"Esc during execution opens the interrupt confirmation. Read the target before confirming; /undo separately previews rewinding the latest conversation turn.",zh:"执行中按 Esc 打开中断确认，确认前核对目标；/undo 则单独预览撤回最近一轮会话。"}),
 ]};
}
export function helpKeys(scene:Scene,t:Translate){
 const rows=[
  {keys:key("slash"),meaning:t({en:"Open command suggestions. Arrow keys select; Enter fills or runs the selected command; Esc returns.",zh:"打开命令推荐；方向键选择，Enter 填入或执行所选命令，Esc 返回。"})},
  {keys:key("pageUp")+" / "+key("pageDown"),meaning:t({en:"Scroll the visible conversation or panel. The mouse wheel also scrolls.",zh:"滚动当前对话或面板，也可使用鼠标滚轮。"})},
 ];
 if(scene==="projects"||scene==="process_list")rows.push(
  {keys:key("upDown")+" · "+key("enter"),meaning:t({en:"Select a list row and open it.",zh:"上下选择列表项，Enter 打开。"})},
  ...(scene==="process_list"?[{keys:key("tab")+" / "+key("escape"),meaning:t({en:"Return to the main conversation.",zh:"返回主进程对话。"})}]:[]),
 );
 else rows.push(
  {keys:key("enter")+" · "+key("shiftEnter"),meaning:t({en:"Send the draft; insert a new line.",zh:"发送草稿；插入换行。"})},
  {keys:key("leftRight")+" · "+key("home")+" / "+key("end"),meaning:t({en:"Move the text cursor; move to the start or end of the draft.",zh:"移动文本光标；移到草稿开头或结尾。"})},
  {keys:key("ctrlLeft")+" / "+key("ctrlRight"),meaning:t({en:"Move the cursor by word.",zh:"按词移动光标。"})},
  {keys:key("left"),meaning:scene==="workspace"
    ?t({en:"With an empty editor outside editing, return to the project list. With a draft, this only moves the cursor.",zh:"输入为空且未编辑时返回项目列表；有草稿时只移动光标。"})
    :t({en:"With an empty editor outside editing, return to the main conversation or subprocess list you came from. With a draft, this only moves the cursor.",zh:"输入为空且未编辑时返回进入前的主对话或子进程列表；有草稿时只移动光标。"})},
  {keys:key("ctrlG"),meaning:t({en:"Edit the complete prompt in an external editor.",zh:"在外部编辑器中编辑完整输入内容。"})},
  {keys:key("escape"),meaning:t({en:"During execution, open interrupt confirmation. In an overlay, return or close; inside an editor, leave editing before navigating back.",zh:"执行中打开中断确认；面板内返回或关闭；编辑时先离开编辑状态，再返回上层。"})},
  ...(scene==="workspace"?[{keys:key("tab")+" · "+key("leftRight")+" · "+key("enter"),meaning:t({en:"When working/waiting subprocesses appear at the bottom: focus the strip, select a subprocess, then open it.",zh:"底部有工作中或等待中的子进程时：Tab 聚焦，左右选择，Enter 打开。"})}]:[]),
 );
 rows.push({keys:applicationBindings.label("clipboard.copy")+" / "+applicationBindings.label("clipboard.paste"),meaning:t({en:"Copy selected text; paste literal text into the focused editor.",zh:"复制所选文字；将文字粘贴到当前输入框。"})},
 {keys:key("leftRight")+" · "+key("upDown")+" · "+key("escape"),meaning:t({en:"In this help: switch tabs, scroll the text, then return to tabs or close.",zh:"在本帮助页中：左右切换页签，上下滚动正文，Esc 返回页签或关闭。"})});
 return rows;
}
