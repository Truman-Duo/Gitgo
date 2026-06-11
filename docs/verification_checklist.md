# Claude Code 验证方法 — Gitgo 前端补全

> 逐一验证 7 个 Task 的功能正确性。每步含预期结果，不通过则记录为 bug。

---

## 启动

```bash
python -m gitgo                     # GUI 模式
# 或双击 dist/gitgo.exe
```

打开一个已有项目进入工作区。

---

## V1：Authorship Toggle（Task A）

**步骤：**

1. 确认 Workshop Tab 底部 Push 按钮右侧出现 `☑ 清洗 AI 痕迹` checkbox，默认勾选
2. 取消勾选，点 Push（如果无可 push 的 commit，点 Sync 先同步一个再 push）
3. 看底部 log bar：**不应**出现 "AI 痕迹已清洗" 字样
4. 重新勾选，再次 Push
5. 看底部 log bar：**应出现** "AI 痕迹已清洗 (N 处)"

**预期：**

```
checkbox 勾选 → push log 包含 "AI 痕迹已清洗"
checkbox 取消 → push log 不包含
安全警告对话框出现时选「强制 push」→ 也应执行清洗
```

---

## V2：Undo Merge 按钮接通（Task B）

**步骤：**

1. Workshop Tab → Action Bar 点击「Undo Merge」
2. 当前无选中 formal commit → log bar 应显示 "没有可撤销的正式 commit"
3. 选中一个通过 merge 创建的 formal commit（点击 fm column 里的某个卡片）
4. 再次点击「Undo Merge」
5. 弹出确认对话框 "确认 Dissolve"，点 Yes
6. formal commit 应消失，对应的 workspace commit 应恢复未合并态

**预期：**

```
无选中 → log "没有可撤销的正式 commit"
有选中 → 弹出确认框 → Yes 后 formal 消失、workspace 恢复
```

---

## V3：Template 下拉（Task C）

**步骤：**

1. Workshop Tab 底部，Authorship checkbox 右侧出现 `Template:` 标签 + 下拉框
2. 下拉框至少包含 `default` 选项
3. 选择不同模板后，选中几个 workspace commit 点「合并」
4. msg_box 中生成的 commit message 模板格式应随选择变化

**预期：**

```
下拉框默认选中 "default"
切换模板 → merge 生成的模板内容不同
（验证方式：选 default 合并看模板 → 撤销 → 换模板再合并看差异）
```

---

## V4：Governance Tab 显示（Task D/E/F）

**步骤：**

1. Tab 栏第 5 位出现「治理」标签
2. 点击进入 Governance Tab
3. 应出现三张卡片：Project Contract / Identity Guard / Lesson System
4. 每张卡片有标题、状态 pill、内容行和操作按钮

**预期：**

```
Tab 栏: [Workshop] [Incoming] [Remotes] [History] [Governance]
卡片 1: Contract — 显示版本/功能数/约束数，有 "View Contract" 按钮
卡片 2: Identity — 显示百分比进度条 + 检查结果
卡片 3: Lesson — 显示 Abstract/Instance/Pending 三个数字 + 最近 lesson 列表
```

**可能的问题（不是 bug）：**

```
没有 .gitgo/contract.yaml → Contract 卡片显示 "Not found" pill — 正常
没有 lesson 数据 → Lesson 卡片数字全 0 — 正常
Identity 检查依赖 scan entries → 若未 scan 则可能全通过 — 正常
```

---

## V5：Contract 查看弹窗（Task E）

**步骤：**

1. Governance Tab → Contract 卡片 → 点击「View Contract」
2. 弹出只读对话框，显示完整合约内容

**预期：**

```
弹窗出现，标题 "Project Contract"
内容为 YAML/JSON 格式文本
可滚动查看
```

---

## V6：History 治理事件切换器（Task G）

**步骤：**

1. 进入 History Tab
2. 顶部出现下拉切换器（QComboBox），默认选中「Commits」
3. 下拉选择「Governance Events」
4. 列表切换为治理事件：sync / push / drift_detected 等
5. 每种事件有颜色 pill（sync=绿、push=蓝、drift=红等）

**预期：**

```
[Commits ▼] 切换器在页面顶部
切换为 "Governance Events" → 条目颜色各异
至少有 sync/push 类型的条目（如果项目有过操作）
日期分隔头正常显示
```

---

## V7：Action Bar 在 Governance Tab（Task F）

**步骤：**

1. 切换到 Governance Tab
2. 观察 Action Bar：应只有最右侧一个 「↻」按钮
3. 点击「↻」→ 治理数据应刷新（卡片重新加载）

**预期：**

```
切换到 Governance Tab → Action Bar 只有 ↻
点击 ↻ → 卡片内容刷新（若数据没变则外观不变也是正常的）
```

---

## 不通过项目录

| 编号 | 验证项 | 不通过现象 | 排查方向 |
|------|--------|-----------|----------|
| | | | |

---

## 快速验证脚本（可选）

在项目根目录创建 `_verify_imports.py`，运行 `python _verify_imports.py` 确认所有模块可导入：

```python
"""快速验证：所有改动不破坏 import 链"""
import sys
sys.path.insert(0, ".")

# Round 1
from frontend.workspace.workshop_tab import WorkshopTabMixin
from frontend.workspace.syncpush import SyncPushMixin

# Round 2
from frontend.workspace.panel_state import PanelState
ps = PanelState()
assert hasattr(ps, "contract_data"), "Missing contract_data"
assert hasattr(ps, "integrity_status"), "Missing integrity_status"
assert hasattr(ps, "lesson_data"), "Missing lesson_data"

from frontend.workspace.governance import GovernanceMixin
from frontend.workspace.builder import BuilderMixin  # 会触发 governance import

# locales
import json
for lf in ["locales/zh.json", "locales/en.json"]:
    with open(lf, encoding="utf-8") as f:
        d = json.load(f)
    for k in ["tab.governance", "gov.contract", "gov.identity",
              "action.strip_authorship", "action.template",
              "action.undo_none", "history.filter_commits"]:
        assert k in d, f"Missing key {k} in {lf}"

print("All checks passed.")
```
