import type {Copy} from "./pageContract.js";
import {applicationBindings} from "../input/router.js";
import {chordLabel} from "../input/bindings.js";
const guides={
  "create": {
    "title": {
      "en": "Create project",
      "zh": "创建项目"
    },
    "purpose": {
      "en": "Configure a project workspace.",
      "zh": "配置项目工作目录。"
    },
    "steps": [
      {
        "en": "Enter edits or stages one field; Ctrl+S submits the complete form. Attach Existing requires an existing folder; Create New requires a missing or empty folder. Uses the configured active provider.",
        "zh": "Enter 编辑或暂存字段，Ctrl+S 提交整张表单。接入已有目录要求目录存在；创建新目录要求路径不存在或为空；模型沿用当前服务商。",
        "keys": true
      }
    ]
  },
  "export": {
    "title": {
      "en": "Export",
      "zh": "导出"
    },
    "purpose": {
      "en": "Choose export scope, format and destination.",
      "zh": "选择导出范围、格式及目标路径。"
    },
    "steps": [
      {
        "en": "Enter edits or stages a field; Ctrl+S exports the selected scope and format to the displayed path. Errors retain the draft; Enter or Esc closes a completed result.",
        "zh": "Enter 编辑或暂存字段，Ctrl+S 按显示的范围、格式及路径导出；失败保留草稿；导出成功后 Enter 或 Esc 关闭。",
        "keys": true
      }
    ]
  },
  "usage": {
    "title": {
      "en": "Usage",
      "zh": "用量统计"
    },
    "purpose": {
      "en": "Browse recorded usage by date, task or project.",
      "zh": "按日期、任务或项目浏览实际用量记录。"
    },
    "steps": [
      {
        "en": "Enter opens the focused header. Left/right changes headers; Tab moves between content and actions after entry. Tasks use their start date. Cache reads are not added twice. Heat colors use nonzero-day quartiles; unavailable projects make the total partial. Unrecorded costs are not billing estimates.",
        "zh": "Enter 进入所选页签，左右切换页签；进入后 Tab 在内容与操作间切换。任务按开始日期归集，缓存读取不重复计入；热图按非零用量日期四分位着色。项目不可用时汇总不完整，未记录费用不作账单估算。",
        "keys": true
      },
      {
        "en": "Enter opens the selected project's waterfall.",
        "zh": "按 Enter 打开选中项目的逐轮瀑布图。",
        "keys": true
      },
      {
        "en": "←/→ tabs · Enter browse · Esc back",
        "zh": "←/→ 页签 · Enter 浏览 · Esc 返回",
        "keys": true
      },
      {
        "en": "←/→ actions · Enter apply · ↑ content · Esc tabs",
        "zh": "←/→ 操作 · Enter 执行 · ↑ 内容 · Esc 页签",
        "keys": true
      },
      {
        "en": "Arrows select day · PgUp/PgDn scroll · Tab actions · Esc tabs",
        "zh": "方向键选择日期 · PgUp/PgDn 滚动 · Tab 操作 · Esc 页签",
        "keys": true
      },
      {
        "en": "↑/↓ select · Enter project · Tab actions · Esc tabs",
        "zh": "↑/↓ 选择 · Enter 项目 · Tab 操作 · Esc 页签",
        "keys": true
      }
    ]
  },
  "trace": {
    "title": {
      "en": "Turn waterfall",
      "zh": "逐轮瀑布图"
    },
    "purpose": {
      "en": "Inspect recorded turns, requests and their evidence.",
      "zh": "查看已记录轮次、请求及其证据。"
    },
    "steps": [
      {
        "en": "←/→ actions · Enter apply · Tab chart · Esc chart",
        "zh": "←/→ 操作 · Enter 执行 · Tab 图表 · Esc 图表",
        "keys": true
      },
      {
        "en": "←/→ turn · ↑/↓ or PgUp/PgDn scroll · Tab actions · Esc back",
        "zh": "←/→ 轮次 · ↑/↓ 或 PgUp/PgDn 滚动 · Tab 操作 · Esc 返回",
        "keys": true
      }
    ]
  },
  "update": {
    "title": {
      "en": "Software update",
      "zh": "软件更新"
    },
    "purpose": {
      "en": "Inspect installation capability and review download, update or rollback actions.",
      "zh": "查看安装能力，核对下载、更新或回退操作。"
    },
    "steps": [
      {
        "en": "Read-only/source installations can inspect status but cannot self-replace. Disabled actions show the actual reason. Up/down, PgUp/PgDn and the wheel browse overflow even when only Refresh is available. Downloading, applying/restarting, rollback and cancelling a pending replacement require their displayed explicit confirmations.",
        "zh": "只读或源码安装可检查状态，但不能自替换；不可用动作显示真实原因。仅剩检查状态时，上下、PgUp/PgDn 和滚轮仍可浏览超长内容。下载、应用并重启、回退及取消待替换各自需要显示的明确确认。",
        "keys": true
      }
    ]
  },
  "config": {
    "title": {
      "en": "General configuration",
      "zh": "通用配置"
    },
    "purpose": {
      "en": "Select a setting, edit it and confirm the change.",
      "zh": "选择设置，进入编辑并确认修改。"
    },
    "steps": [
      {
        "en": "Enter save · Esc cancel",
        "zh": "Enter 保存 · Esc 取消",
        "keys": true
      },
      {
        "en": "←/→ choose · Enter save · Esc cancel",
        "zh": "←/→ 选择 · Enter 保存 · Esc 取消",
        "keys": true
      },
      {
        "en": "↑↓ select · Enter change · Esc tabs",
        "zh": "↑↓ 选择 · Enter 修改 · Esc 页签",
        "keys": true
      },
      {
        "en": "←/→ tabs · Enter open · Esc back",
        "zh": "←/→ 页签 · Enter 打开 · Esc 返回",
        "keys": true
      },
      {
        "en": "First launch · choose a terminal and press Enter. ",
        "zh": "首次启动：选择终端并按 Enter。 ",
        "keys": true
      },
      {
        "en": "Changes apply on the next launch. ",
        "zh": "更改在下次启动时生效。 ",
        "keys": true
      }
    ]
  },
  "keybindings": {
    "title": {
      "en": "Key bindings",
      "zh": "绑键"
    },
    "purpose": {
      "en": "Inspect, detect and change the actual input bindings.",
      "zh": "查看、检测及修改真实输入绑定。"
    },
    "steps": [
      {
        "en": "Select an action and confirm to rebind.",
        "zh": "选中动作并确认以换绑。",
        "keys": true
      },
      {
        "en": "restores the selected default",
        "zh": "恢复所选动作默认键",
        "keys": true
      },
      {
        "en": "Detection shows only events delivered by this terminal. Collapsed combinations cannot be distinguished.",
        "zh": "检测仅显示终端实际送达的键；被折叠为同一事件的组合键无法区分。",
        "keys": true
      }
    ]
  },
  "runtime": {
    "title": {
      "en": "Runtime",
      "zh": "运行信息"
    },
    "purpose": {
      "en": "Choose a runtime section; only Enter transfers focus into its content.",
      "zh": "选择运行信息页签；只有 Enter 将焦点移入内容。"
    },
    "steps": [
      {
        "en": "Enter open",
        "zh": "Enter 打开",
        "keys": true
      }
    ]
  },
  "context": {
    "title": {
      "en": "Context",
      "zh": "上下文"
    },
    "purpose": {
      "en": "Inspect the selected conversation context and its recorded sources.",
      "zh": "查看所选会话上下文及已记录来源。"
    },
    "steps": [
      {
        "en": "←/→ process · ↑/↓ scroll · Esc back",
        "zh": "←/→ 进程 · ↑/↓ 滚动 · Esc 返回",
        "keys": true
      }
    ]
  },
  "governance": {
    "title": {
      "en": "Governance",
      "zh": "治理信息"
    },
    "purpose": {
      "en": "Inspect Quality, Patterns, Feed and Releases observations.",
      "zh": "查看质量、模式、事件流及版本观察。"
    },
    "steps": [
      {
        "en": "Left/right changes sections; up/down scrolls. In Feed, PgUp reads newer saved pages and PgDn reads older pages. Page counts and loading state describe the current observation.",
        "zh": "左右切换分区，上下滚动；事件流中 PgUp 查看较新页，PgDn 查看较旧页。页数与加载状态表示当前观察。",
        "keys": true
      }
    ]
  },
  "memory": {
    "title": {
      "en": "Memory snapshots",
      "zh": "记忆快照"
    },
    "purpose": {
      "en": "Create or explicitly restore a selected snapshot.",
      "zh": "创建快照，或明确恢复选中快照。"
    },
    "steps": [
      {
        "en": "↑/↓ select · Tab actions/list · ←/→ action · Enter open · Esc back",
        "zh": "↑/↓ 选择 · Tab 操作/列表 · ←/→ 操作 · Enter 打开 · Esc 返回",
        "keys": true
      }
    ]
  },
  "tools": {
    "title": {
      "en": "Runtime tools",
      "zh": "运行时工具"
    },
    "purpose": {
      "en": "Inspect tool details, then review archive or restore actions.",
      "zh": "查看工具详情，再核对归档或恢复动作。"
    },
    "steps": [
      {
        "en": "Enter confirm · Esc cancel",
        "zh": "Enter 确认 · Esc 取消",
        "keys": true
      },
      {
        "en": "Enter review archive/restore · Esc list",
        "zh": "Enter 确认归档/恢复 · Esc 列表",
        "keys": true
      },
      {
        "en": "↑/↓ select · Enter details · Esc tabs",
        "zh": "↑/↓ 选择 · Enter 详情 · Esc 页签",
        "keys": true
      }
    ]
  },
  "mechanisms": {
    "title": {
      "en": "Task mechanisms",
      "zh": "任务机制"
    },
    "purpose": {
      "en": "Inspect proposals, dependencies and recorded revisions.",
      "zh": "查看提案、依赖及已记录修订。"
    },
    "steps": [
      {
        "en": "Up/down selects a proposal, Enter opens details and R refreshes. Left/right selects a currently available action. Suspension or withdrawal does not remove obligations already accepted by the main task; changes require explicit confirmation.",
        "zh": "上下选择提案，Enter 打开详情，R 刷新；左右选择当前可用动作。暂停或撤回不移除主任务已接受的义务，变更仍需明确确认。",
        "keys": true
      }
    ]
  },
  "lessons": {
    "title": {
      "en": "Project lessons",
      "zh": "项目经验"
    },
    "purpose": {
      "en": "Review, edit and apply lessons from this project. Gitgo internal faults are bugs.",
      "zh": "审阅、编辑及采用本项目经验；Gitgo 内部故障属于 bug。"
    },
    "steps": [
      {
        "en": "Tab moves through trigger, rule, type, conditions, validation, novelty and limitations.",
        "zh": "Tab 切换触发条件、规则、类型、适用条件、验证、新颖性和限制。",
        "keys": true
      },
      {
        "en": "PgUp/PgDn scroll · Esc return to review",
        "zh": "PgUp/PgDn 滚动 · Esc 返回审阅",
        "keys": true
      },
      {
        "en": "Enter search · Esc back",
        "zh": "Enter 搜索 · Esc 返回",
        "keys": true
      },
      {
        "en": "Tab list/actions · ←/→ action · ↑/↓ select · Enter open · Esc back",
        "zh": "Tab 列表/操作 · ←/→ 操作 · ↑/↓ 选择 · Enter 打开 · Esc 返回",
        "keys": true
      }
    ]
  },
  "trial": {
    "title": {
      "en": "Trial contributions",
      "zh": "待接手贡献"
    },
    "purpose": {
      "en": "Inspect incoming contributions and explicitly select their handling action.",
      "zh": "查看待接手贡献并明确选择处理动作。"
    },
    "steps": []
  },
  "formal": {
    "title": {
      "en": "Formal commits",
      "zh": "正式提交"
    },
    "purpose": {
      "en": "Inspect formal commits and review deletion or dissolution effects.",
      "zh": "查看正式提交并核对删除或解散的效果。"
    },
    "steps": []
  },
  "btw": {
    "title": {
      "en": "BTW",
      "zh": "BTW"
    },
    "purpose": {
      "en": "Side discussions are read-only by default; adopting work requires an explicit target and action.",
      "zh": "侧讨论默认只读；纳入工作需要明确目标与动作。"
    },
    "steps": [
      {
        "en": "Alt+M previous region · ",
        "zh": "Alt+M 上一区域 · ",
        "keys": true
      },
      {
        "en": "Parent conversation · ↑/↓ or PgUp/PgDn browse · Tab discussion · Esc close",
        "zh": "主/子会话 · ↑/↓ 或 PgUp/PgDn 浏览 · Tab 返回讨论 · Esc 关闭",
        "keys": true
      },
      {
        "en": "↑/↓ scroll · type to reply · Tab switch · Esc close",
        "zh": "↑/↓ 滚动 · 直接输入追问 · Tab 切换 · Esc 关闭",
        "keys": true
      },
      {
        "en": "Enter ask · ↑/↓ scroll · Tab actions · Esc close",
        "zh": "Enter 提问 · ↑/↓ 滚动 · Tab 操作 · Esc 关闭",
        "keys": true
      },
      {
        "en": "←/→ actions · Enter apply · Tab parent · Esc close",
        "zh": "←/→ 操作 · Enter 执行 · Tab 主会话 · Esc 关闭",
        "keys": true
      }
    ]
  },
  "quit": {
    "title": {
      "en": "Quit Gitgo",
      "zh": "退出 Gitgo"
    },
    "purpose": {
      "en": "Choose Quit or Cancel. Quitting closes the current Host and its managed processes.",
      "zh": "选择退出或取消；退出会关闭当前宿主及其受管进程。"
    },
    "steps": [
      {
        "en": "↑/↓ select · Enter confirm · Esc cancel",
        "zh": "↑/↓ 选择 · Enter 确认 · Esc 取消",
        "keys": true
      }
    ]
  },
  "undo": {
    "title": {
      "en": "Session rewind",
      "zh": "会话撤回"
    },
    "purpose": {
      "en": "Rewind the latest conversation turn; workspace changes and external effects remain.",
      "zh": "撤回最近一轮对话，保留工作区改动与已发生的外部效果。"
    },
    "steps": [
      {
        "en": "←/→ choose · Enter confirm · Esc cancel",
        "zh": "←/→ 选择 · Enter 确认 · Esc 取消",
        "keys": true
      }
    ]
  },
  "compact": {
    "title": {
      "en": "Context compaction",
      "zh": "上下文压缩"
    },
    "purpose": {
      "en": "Inspect the current compaction attempt and any required user decision.",
      "zh": "查看本次上下文压缩及需要用户决定的事项。"
    },
    "steps": [
      {
        "en": "An in-progress attempt is not success. A required reset/stop decision is shown explicitly; confirming uses the existing maintenance boundary and does not grant unrelated authority. Esc closes an idle result.",
        "zh": "正在处理不代表成功；需要重置或停止时明确展示决定，经现有维护边界确认，不授予无关权限；空闲结果可按 Esc 关闭。",
        "keys": true
      }
    ]
  },
  "bin": {
    "title": {
      "en": "Archive and deletion",
      "zh": "归档与删除"
    },
    "purpose": {
      "en": "Review archived targets and deletion plans; inspect effects before confirming.",
      "zh": "查看归档目标与删除计划，确认前核对影响范围。"
    },
    "steps": [
      {
        "en": "Left/right changes the target, plan or delay section; up/down selects a row. Enter edits a delay and left/right adjusts it; Enter saves, Esc cancels. /restore restores an archive; /soft-delete and /hard-delete prepare an effect preview. /cancel and /retry operate on the selected saved plan. Deletion still requires a separate explicit confirmation.",
        "zh": "左右切换目标、计划或延迟分区，上下选择行。Enter 编辑延迟，左右调整，Enter 保存、Esc 取消。/restore 恢复归档；/soft-delete、/hard-delete 先准备影响预览；/cancel、/retry 操作选中计划。删除仍需单独明确确认。",
        "keys": true
      }
    ]
  }
} as const;
export type MenuHelpTopic=keyof typeof guides;
export function menuHelpGuide(topic:string,t:(copy:Copy)=>string){
 const guide=guides[topic as MenuHelpTopic];if(!guide)return null;
 const steps:string[]=guide.steps.map(value=>t(value));
 if(topic==="update")steps.push(t({en:"Only Gitgo software is replaced. Automatic installation waits for a clean exit; tasks and other instances are never forcibly stopped. Rollback requires its own confirmation.",zh:"仅替换 Gitgo 软件；自动安装等待正常退出，不会强制结束任务或其他实例；回退仍需单独确认。"}));
 if(topic==="keybindings")steps.unshift(t({en:`${chordLabel("enter")} opens capture for the selected action; ${chordLabel("delete")} restores its default. During raw capture, physical Enter saves the candidate and physical Esc cancels.`,zh:`${chordLabel("enter")} 进入所选动作的捕获，${chordLabel("delete")} 恢复其默认键；原始捕获中使用实际 Enter 保存候选、实际 Esc 取消。`}));
 if(topic==="trial")steps.push(applicationBindings.label("trial.accept")+" "+t({en:"accept",zh:"接受"})+" · "+applicationBindings.label("trial.promote")+" "+t({en:"promote",zh:"提升"})+" · "+applicationBindings.label("trial.delete")+" "+t({en:"delete",zh:"删除"}));
 if(topic==="formal")steps.push(applicationBindings.label("formal.delete")+" "+t({en:"delete",zh:"删除"})+" · "+applicationBindings.label("formal.dissolve")+" "+t({en:"dissolve",zh:"解散"}));
 if(topic==="lessons")steps.push(applicationBindings.label("lesson.toggle")+" "+t({en:"includes/excludes the selected candidate; only reviewed project lessons may be applied.",zh:"纳入或排除所选候选；只有已审阅的项目经验才可采用。"}));
 steps.push(t({en:"Invoke /help outside text editing. Closing it preserves the original draft, selection and scroll position. Text and JSON editors treat /help literally.",zh:"退出文本编辑后使用 /help；关闭帮助保留原草稿、选择和滚动位置。文本及 JSON 编辑中的 /help 是字面内容。"}));
 return {title:t(guide.title),purpose:t(guide.purpose),steps};
}
