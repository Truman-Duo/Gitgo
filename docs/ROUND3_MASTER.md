# Round 3 — 主执行脚本

按顺序逐 Task 执行。每完成一个 Task，用 grep 验证后继续下一个。

```
执行顺序:
  1. docs/task_H.md    (Bug fix, 1 文件, 2 行改)
  2. docs/task_I.md    (Memory 管理, 1 文件, +80 行)
  3. docs/task_J.md    (Lesson 弹窗, 2 文件+1 新文件)
  4. docs/task_K.md    (Contract 更新, 1 文件, +4 行 + 1 方法)
  5. docs/task_L.md    (Template CRUD, 1 文件, +6 行 + 1 方法)
  6. docs/task_i18n.md (i18n, 2 文件, 47 key × 2)
```

## 每个 Task 的标准流程

```
1. 读 docs/task_X.md
2. Read 提示的源文件行号，确认当前代码匹配 old_string
3. Edit 或 Write 执行改动
4. 运行 grep 验证命令
5. 报告: "Task X done — [验证结果]"
```

## 全部完成后

```bash
python -c "
from frontend.workspace.workshop_tab import WorkshopTabMixin
from frontend.workspace.governance import GovernanceMixin
from frontend.lesson_dialog import LessonDialog
print('All imports OK')
"
```

然后 `python build.py --debug` 打包测试。
