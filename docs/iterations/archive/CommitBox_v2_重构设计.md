# CommitBox / CommitCanvas v2 重构设计

> 日期：2026-05-12
> 范围：`commit_box.py`、`commit_canvas.py`、`workshop_tab.py`、`commits.py`、`themes/qss.py`、`themes/__init__.py`、`frontend/workspace/theme.py`
> 目标：消除三层样式系统冲突（Global QSS ↔ Widget setStyleSheet ↔ QPainter），对齐 v2 HTML 设计，一揽子修复所有已知 bug

---

## 一、当前架构的根因分析

### 1.1 三层样式互相打架

```
Layer 1: app.setStyleSheet()       ← themes/qss.py，全局 QSS，优先级最低但覆盖面最广
Layer 2: widget.setStyleSheet()    ← commit_box.py _apply_style()，每次状态变化调用，覆盖 layer 1
Layer 3: QPainter paintEvent()     ← commit_box.py:263 fillRect，不参与 QSS 层级
```

**冲突链路**：每个 box 调 `setStyleSheet()` → Qt 触发该 widget 及所有子孙的样式重算 → 若父级 Canvas 也被影响则触发 `paintEvent` 重绘贝塞尔线 → 刷新 formal box 列表时几十次连发 → 可见闪烁

**优先级混乱**：`QWidget { background-color: t.bg }`（Layer 1）vs `QFrame { background: t.bg }`（Layer 2）——两者都设背景，但 `background-color` 和 `background` 在 Qt QSS 中是不同的 CSS 属性，行为不完全等价

### 1.2 已确认的所有 Bug 及对应根因

| # | Bug | 根因层级 | 具体位置 |
|---|-----|---------|---------|
| 1 | Canvas 闪烁 | Layer 1↔2 冲突 | 每 box 调 `setStyleSheet()` 触发 Qt 级联重算 → Canvas `paintEvent` 被反复触发 |
| 2 | WS box 文字被遮盖 | Layer 2 不一致 | `_apply_style()` 默认态无 `padding-right`，hover 态有 `padding-right:20px` |
| 3 | 标题与 box 不对齐 | Layer 1 覆盖布局 | QSS `QScrollArea { border: .5px }` 覆盖了 `setFrameShape(NoFrame)` → Canvas 内容区比标题行窄 |
| 4 | Formal box 高亮残留 | Layer 2 状态竞争 | `CommitBox.enterEvent` 设 QFrame-only stylesheet（无 QLabel 规则）→ 覆盖 `_apply_style()` 的 QLabel 规则 |
| 5 | 主题切换后颜色残留 | Layer 2 未刷新 | `_apply_theme_colors()` 遍历 box 调 `_apply_style()` 但未覆盖 `enterEvent` 设的临时 stylesheet |
| 6 | Formal box paintEvent 竖线 | Layer 3 独立 | `fillRect` 绘制 3px 竖线，不参与 QSS，边框/背景变化时竖线可能与 QSS 渲染不同步 |

---

## 二、目标架构：单一 QSS 驱动

### 2.1 核心原则

参考节点状态面板（`explorer.py:82-93`）的可靠模式：

> **每个子 widget 只设 inline stylesheet 一次（`__init__` 中），状态变化通过 `setProperty` + `unpolish/polish` 触发 QSS 重算。永远不动态覆写 `setStyleSheet()`。永远不用 QPainter 做装饰性绘制。**

### 2.2 新三层（单一样式源）

```
Layer 1 ONLY: app.setStyleSheet()         ← themes/qss.py，全局唯一的样式定义源
                 ↑ property selector 驱动状态
Layer 数据:    widget.setProperty()       ← 如 setProperty("selected", True)
                 ↑ unpolish/polish 触发重绘
Layer 渲染:    Qt 内置 QStyle             ← Qt 自动处理，不再手写 QPainter
```

**关键改变**：
- 删除所有 box 的 `_apply_style()` 方法和 `setStyleSheet()` 调用
- 把原来 `_apply_style()` 里的颜色逻辑全部移到 `themes/qss.py` 的全局 QSS 中，用 property selector 表达
- 状态切换改为 `setProperty("selected", True)` + `style().unpolish(self)` + `style().polish(self)`
- `paintEvent` 只保留贝塞尔线绘制（CommitCanvas），删除 CommitBox 里的 `fillRect` 竖线

---

## 三、文件改动清单

### 3.1 `themes/qss.py` — 全局 QSS（增 ~35 行，删 1 行）

**删除**：
- 第 91 行 `QScrollArea { border: .5px solid {t.bdr}; border-radius: 4px; }` ——这是导致 Canvas 偏移 + 多处意外边框的根源。QScrollArea 的边框由各个使用处的 `setFrameShape()` 自行控制。

**新增 CommitBox 相关规则**：

```css
/* ── Commit Canvas ──────────────────────────────────── */
QWidget#commit_canvas {
    background: {t.bg};
}
/* Canvas 的背景色与 workspace 背景一致，不单独用 bg2 */

QScrollArea#commit_scroll {
    border: none;
    background: {t.bg};
}
/* 显式覆盖，确保 Canvas 外无意外边框或边距 */

/* ── Workspace Commit Box ────────────────────────────── */
QFrame#ws_card {
    background: {t.bg};
    border: 0.5px solid {t.bdr};
    border-radius: 5px;
}
QFrame#ws_card:hover {
    background: {t.bg2};
    border-color: {t.bdr2};
}
QFrame#ws_card[selected="true"] {
    background: {t.blue_bg};
    border-color: {t.blue};
}
QFrame#ws_card[merged="true"] {
    background: {t.bg3};
    border-color: {t.bdr2};
}
/* 子 Label 样式——QSS 不支持父选择器反查，改用独立 objectName */
QLabel#ws_badge {
    font-size: 9px; font-weight: 500; color: {t.blue_txt}; background: transparent;
}
QLabel#ws_summary {
    font-size: 11px; color: {t.txt}; background: transparent; padding-right: 22px;
}
QLabel#ws_meta {
    font-size: 10px; color: {t.txt3}; background: transparent;
}
QLabel#ws_check {
    font-size: 10px; color: transparent; background: transparent;
}
QLabel#ws_check[checked="true"] {
    color: {t.blue_txt};
}
QFrame#ws_card[merged="true"] QLabel#ws_badge,
QFrame#ws_card[merged="true"] QLabel#ws_summary,
QFrame#ws_card[merged="true"] QLabel#ws_meta {
    color: {t.txt3};
}
QFrame#ws_card[selected="true"] QLabel#ws_badge {
    color: {t.blue_txt};
}

/* ── Formal Commit Box ───────────────────────────────── */
QFrame#fm_card {
    background: {t.bg};
    border: 0.5px solid {t.bdr};
    border-radius: 5px;
    border-left: 3px solid {t.blue};        /* ← 替代 QPainter fillRect */
}
QFrame#fm_card:hover {
    background: {t.bg2};
    border-color: {t.bdr2};
    border-left-color: {t.blue};
}
QFrame#fm_card[selected="true"] {
    border-color: {t.blue};
    border-left-color: {t.blue};
    /* 背景不变，文字不变——只改变边框 */
}
QFrame#fm_card[synced="true"] {
    border-left-color: {t.success};
}
QFrame#fm_card[pushed="true"] {
    border-left-color: {t.success_txt};
}
QFrame#fm_card[incoming="true"] {
    border-left-color: {t.amber};
}
QFrame#fm_card[synced="true"] {
    background: {t.success_bg};
}
QFrame#fm_card[pushed="true"] {
    background: {t.success_bg};
}
/* Formal box 内部 label */
QLabel#fm_title {
    font-size: 12px; color: {t.txt}; background: transparent;
}
QLabel#fm_sub {
    font-size: 10px; color: {t.txt3}; background: transparent;
}
/* selected 时不改变文字颜色 */
QFrame#fm_card[selected="true"] QLabel#fm_title {
    color: {t.txt};
}
QFrame#fm_card[selected="true"] QLabel#fm_sub {
    color: {t.txt3};
}
```

**注意**：Qt QSS 对 `QFrame#parent[attr] QLabel#child` 这种父属性选择器支持有限（Qt 5.15+/6.x 支持部分，但不稳定）。**更保险的做法**是把 selected/merged/synced/pushed 这些 property 同时设到子 QLabel 上。详见 3.2。

### 3.2 `commit_box.py` — 完全重写（~180 行 → ~110 行）

**删除**：
- `CommitBox` 基类（不再需要）
- 所有 `_apply_style()` 方法
- 所有 `enterEvent` / `leaveEvent` 覆写（QSS `:hover` 替代）
- `FormalCommitBox.paintEvent`（QSS `border-left` 替代）
- 所有 `setStyleSheet()` 调用

**新结构**：

```
WorkspaceCommitBox(QFrame)
  ├── objectName = "ws_card"
  ├── 属性: selected (bool), merged (bool)  ← 通过 setProperty 驱动
  ├── 子控件:
  │   ├── QLabel#ws_badge    (type badge, 如 "feat")
  │   ├── QLabel#ws_summary  (commit 摘要, wordWrap=True)
  │   ├── QLabel#ws_meta     (hash / 时间)
  │   └── QLabel#ws_check    (右上角 checkbox, 绝对定位)
  └── 公开方法:
      ├── set_selected(bool)  → setProperty + unpolish/polish
      └── set_merged()        → setProperty + unpolish/polish

FormalCommitBox(QFrame)
  ├── objectName = "fm_card"
  ├── 属性: selected, synced, pushed, incoming  ← 全部通过 setProperty
  ├── 子控件:
  │   ├── QLabel#fm_title    (前缀+标题)
  │   ├── QLabel#fm_sub      (状态文本: 已同步/未同步)
  │   └── QPushButton        (⋯ 菜单按钮, 绝对定位)
  └── 公开方法:
      ├── set_selected(bool)
      ├── set_synced(bool)
      ├── set_pushed(bool)
      └── set_incoming(bool)
```

**关键实现细节**：

```python
# WorkspaceCommitBox 状态更新逻辑（伪代码）
def set_selected(self, value: bool):
    self._selected = value
    self.setProperty("selected", value)
    self.cb.setText("✓" if value else "")
    self.cb.setProperty("checked", value)
    # 级联刷新所有子 widget 的样式（Qt 不会自动传播 property 到子控件）
    self._reapply_properties_to_children()
    # 触发 QSS 重算
    self.style().unpolish(self)
    self.style().polish(self)
    for child in [self.type_lbl, self.summary_lbl, self.meta_lbl, self.cb]:
        child.style().unpolish(child)
        child.style().polish(child)

def _reapply_properties_to_children(self):
    """将父级状态同步到子 Label（Qt QSS 父选择器不可靠时的退路）"""
    # 子 Label 也设同样的 property，QSS 可直接用 QLabel#ws_summary[selected="true"]
    for child in [self.summary_lbl]:
        child.setProperty("selected", self._selected)
```

**关于 QSS 父属性选择器的说明**：

Qt 6.x 的 QSS 支持 `QFrame#ws_card[selected="true"] QLabel#ws_summary` 这种写法，但在某些平台/版本下不可靠。本设计采用**双保险**策略：父级设 property + 关键子 Label 也设 property。QSS 中同时保留两种选择器写法（父选择器和子直选），Qt 会自动匹配可用的。

### 3.3 `commit_canvas.py` — 微调

**改动**：
- `lo.setSpacing(52)` 保留
- 删掉 `self.setObjectName("commit_canvas")` 的显式背景设置（改为 QSS 统一管理）
- `paintEvent` 中的贝塞尔线绘制逻辑保留，但增加防护：

```python
def paintEvent(self, event):
    super().paintEvent(event)
    if not self.connections:
        return
    # 检查 columns 是否已经布局完毕（geometry 有效）
    if self.ws_column.geometry().width() == 0:
        return
    # ... 原绘制逻辑
```

### 3.4 `workshop_tab.py` — 微调（~3 行改动）

**改动**：

1. 给 `commit_scroll` 设 objectName：
   ```python
   self.commit_scroll.setObjectName("commit_scroll")
   ```

2. 标题行改为与 Canvas 完全一致的 layout 参数（**CC 已修好，但需确认**）：
   ```python
   hdrs = QHBoxLayout()
   hdrs.setContentsMargins(0, 0, 0, 0)
   hdrs.setSpacing(52)
   self.ws_hdr.setMinimumWidth(148)
   hdrs.addWidget(self.ws_hdr, 0)
   hdrs.addWidget(self.fm_hdr, 1)  # 删除中间 addSpacing
   ```

3. Canvas 创建时无需额外 QSS（QSS 由 themes/qss.py 全局管理）：
   ```python
   self.commit_canvas = CommitCanvas()
   # setObjectName("commit_canvas") 已在 CommitCanvas.__init__ 中设置
   ```

### 3.5 `commits.py` — 简化状态切换逻辑（~15 行改动）

**删除**：
- `_set_active_formal()` 整方法（QGraphicsOpacityEffect 相关全删）
- `_update_formal_box_styles()` 中直接设 `w.selected = True/False` 改为调 `w.set_selected(True/False)`

**修改 `_refresh_formal_boxes` 和 `_refresh_workspace_boxes`**：批量更新外包裹 `setUpdatesEnabled`：

```python
def _refresh_formal_boxes(self):
    self._clear_box_layout(self.fm_box_layout)
    if not self.session.formal_commits:
        # ... placeholder label
        if hasattr(self, 'fm_hdr'):
            self.fm_hdr.setVisible(False)
    else:
        self.fm_container.setUpdatesEnabled(False)  # ← 阻止中间态渲染
        if hasattr(self, 'fm_hdr'):
            self.fm_hdr.setVisible(True)
        for i, fc in enumerate(self.session.formal_commits):
            box = FormalCommitBox(i, ...)
            # 设置初始状态（无 QSS 触发，因为 updatesEnabled=False）
            box.set_synced(fc.synced)
            box.set_pushed(fc.pushed)
            box.clicked.connect(self._on_formal_box_clicked)
            # ...其他信号连接
            self.fm_box_layout.addWidget(box)
        self.fm_container.setUpdatesEnabled(True)   # ← 一次性触发渲染
        self._update_formal_box_styles()
    self.fm_box_layout.addStretch()
    QTimer.singleShot(0, self._refresh_commit_lines)
```

同理包裹 `_refresh_workspace_boxes`。

**修改 `_on_formal_box_clicked`**：删除 `_set_active_formal` 调用，只留 `_update_formal_box_styles`：

```python
def _on_formal_box_clicked(self, index: int):
    if self.selected_formal == index:
        self.selected_formal = None
    else:
        self.selected_formal = index
    self._update_formal_box_styles()
    # _set_active_formal 已删除——不再需要 opacity effect
    self.delete_formal_btn.setEnabled(self.selected_formal is not None)
    # ...
```

### 3.6 `frontend/workspace/theme.py` — 简化主题刷新（~10 行改动）

**删除**：
- `_apply_theme_colors()` 中遍历 box 调 `_apply_style()` 的逻辑（`_apply_style` 方法已不存在）

**改为**：
```python
def _apply_theme_colors(self):
    self._refresh_workshop_styles()
    # CommitBox 的状态通过 QSS 全局管理，主题切换时只需 reapp.setStyleSheet()
    # 新的 QSS 会自动应用到所有 box。但如果 box 的状态 property 没变，
    # Qt 可能不会触发重绘——所以强制 unpolish/polish 关键 widget
    for layout_name in ("ws_box_layout", "fm_box_layout"):
        lo = getattr(self, layout_name, None)
        if lo is None:
            continue
        for i in range(lo.count()):
            w = lo.itemAt(i).widget()
            if w is not None:
                w.style().unpolish(w)
                w.style().polish(w)
    # incoming 样式
    if hasattr(self, '_refresh_incoming_styles'):
        self._refresh_incoming_styles()
```

### 3.7 `themes/__init__.py` — 无需改动

ThemeColors 和 get_theme() 保持不变，QSS 动态生成机制保持不变。

---

## 四、Bug 修复对照表

| Bug | 根因 | 本重构如何修复 |
|-----|------|--------------|
| Canvas 闪烁 | `setStyleSheet` 级联重算 | box 不再调 `setStyleSheet`，状态切换用 `setProperty`+`polish`（Qt 内部优化过的增量重算） |
| WS box 文字遮盖 | 默认态缺 `padding-right` | QSS 统一设 `padding-right: 22px`，所有状态一致 |
| 标题不对齐 | QSS `QScrollArea { border }` | 删除全局 QScrollArea 边框规则 + `#commit_scroll { border: none }` 显式覆盖 |
| Formal box 高亮 | `enterEvent` 写死 QFrame-only stylesheet | 删除所有 enterEvent/leaveEvent，QSS `:hover` + `[selected]` 属性选择器接管 |
| 主题切换残留 | `_apply_style()` 未覆盖 enterEvent 临时样式 | 不再有 enterEvent 临时样式，主题切换只需 unpolish/polish 强制重读 QSS |
| Formal 竖线错位 | QPainter fillRect 不参与 QSS | 改为 QSS `border-left: 3px solid`，与边框/背景同步渲染 |

---

## 五、风险与回退

### 风险
- **QSS 父属性选择器兼容性**：`QFrame#ws_card[selected="true"] QLabel#ws_summary` 在 Qt 6.2- 可能不生效。已用子 Label 也设 property 的双保险策略规避。
- **`setUpdatesEnabled` 包裹影响**：如果中间抛异常，widget 会永久不刷新。需要在 finally 块中强制 `setUpdatesEnabled(True)`。
- **polish/unpolish 性能**：状态切换频繁时（如 Ctrl+点击多选），逐个 unpolish/polish 可能有性能开销。可加防抖（QTimer.singleShot 合并多次 polish）。

### 回退
- 保留 `commit_box.py` 的旧版本为 `commit_box_v0.py`，重构后测试不通过可快速切回
- 每个 sub-task 独立可测试（先改 QSS → 再改 WS box → 再改 Formal box）

---

## 六、实施步骤（6 步，每步独立可测）

### Step 1：清理全局 QSS（`themes/qss.py`）
- 删除 `QScrollArea { border: ... }`
- 新增 `#commit_scroll { border: none }`、`#commit_canvas` 背景覆盖
- 验证：启动 GUI → Canvas 无意外边框

### Step 2：重写 CommitBox QSS 规则（`themes/qss.py`）
- 按 3.1 设计新增全部 `#ws_card` 和 `#fm_card` 规则
- 验证：QSS 语法无误（Python 启动不报错即可）

### Step 3：重写 WorkspaceCommitBox（`commit_box.py`）
- 删除 `_apply_style()`、`enterEvent`、`leaveEvent`
- 改为 `setProperty` + `polish` 驱动
- 验证：WS box 渲染 + 选中/取消选中 + hover + merge

### Step 4：重写 FormalCommitBox（`commit_box.py`）
- 删除 `_apply_style()`、`enterEvent`、`leaveEvent`、`paintEvent`
- 改为 `setProperty` + `polish` 驱动
- QSS `border-left` 替代 QPainter 竖线
- 验证：Formal box 渲染 + 选中高亮（仅边框变蓝）+ synced/pushed 状态色

### Step 5：简化 commits.py + theme.py
- 删除 `_set_active_formal`
- `_update_formal_box_styles` 改为调 `set_selected`
- `_refresh_*_boxes` 外包 `setUpdatesEnabled`
- `theme.py` 简化 `_apply_theme_colors`
- 验证：全流程（加载→选择→合并→sync→push）无视觉异常

### Step 6：对齐 workshop_tab.py 标题行
- 确认 `setSpacing(52)` + `setMinimumWidth(148)` 与 Canvas 一致
- 给 `commit_scroll` 加 objectName
- 验证：标题与下方 columns 像素级对齐

---

## 七、关键代码片段（供 CC 实现参考）

### WorkspaceCommitBox 核心逻辑

```python
class WorkspaceCommitBox(QFrame):
    clicked = Signal(int)

    def __init__(self, index, commit_type, summary, meta, parent=None):
        super().__init__(parent)
        self._idx = index
        self.setObjectName("ws_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(64)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(10, 6, 28, 6)
        lo.setSpacing(2)

        self.type_lbl = QLabel(commit_type.lower())
        self.type_lbl.setObjectName("ws_badge")
        lo.addWidget(self.type_lbl)

        self.summary_lbl = QLabel(summary)
        self.summary_lbl.setObjectName("ws_summary")
        self.summary_lbl.setWordWrap(True)
        lo.addWidget(self.summary_lbl)

        self.meta_lbl = QLabel(meta)
        self.meta_lbl.setObjectName("ws_meta")
        lo.addWidget(self.meta_lbl)

        self.cb = QLabel(self)
        self.cb.setObjectName("ws_check")
        self.cb.setFixedSize(14, 14)
        self.cb.setAlignment(Qt.AlignCenter)
        lo.addWidget(self.cb)  # 不绝对定位，放入 layout 更可控
        # 如果必须保留绝对定位，在 resizeEvent 中 move

    def _set_state(self, selected=False, merged=False):
        self.setProperty("selected", selected)
        self.setProperty("merged", merged)
        self.summary_lbl.setProperty("selected", selected)
        self.summary_lbl.setProperty("merged", merged)
        # polish 触发 QSS 重算
        for w in [self, self.type_lbl, self.summary_lbl,
                   self.meta_lbl, self.cb]:
            w.style().unpolish(w)
            w.style().polish(w)

    def set_selected(self, value):
        self._selected = value
        self.cb.setText("✓" if value else "")
        self.cb.setProperty("checked", value)
        self._set_state(selected=value, merged=self._merged)

    def set_merged(self):
        self._merged = True
        self._selected = False
        self.cb.setText("")
        self._set_state(selected=False, merged=True)

    def mousePressEvent(self, event):
        if not self._merged:
            self.clicked.emit(self._idx)
        super().mousePressEvent(event)
```

### FormalCommitBox 核心逻辑

```python
class FormalCommitBox(QFrame):
    clicked = Signal(int)
    double_clicked = Signal(int, str)
    context_menu = Signal(int, str)

    def __init__(self, index, title, subtitle, parent=None):
        super().__init__(parent)
        self._idx = index
        self.setObjectName("fm_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(56)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(13, 6, 28, 6)  # 左边距+3 给 border-left 让位
        lo.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("fm_title")
        lo.addWidget(self.title_label)

        self.sub_label = QLabel(subtitle)
        self.sub_label.setObjectName("fm_sub")
        lo.addWidget(self.sub_label)

        # ⋯ 按钮
        t = get_theme()
        self.menu_btn = QPushButton("⋯", self)
        self.menu_btn.setFixedSize(18, 18)
        self.menu_btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; border:none; "
            f"color:{t.txt3}; font-size:13px; border-radius:3px; }}"
            f"QPushButton:hover {{ background:{t.bg3}; color:{t.txt}; }}"
        )

    def _polish_all(self):
        for w in [self, self.title_label, self.sub_label]:
            w.style().unpolish(w)
            w.style().polish(w)

    def set_selected(self, value):
        self.setProperty("selected", value)
        self.title_label.setProperty("selected", value)
        self.sub_label.setProperty("selected", value)
        self._polish_all()

    def set_synced(self, value):
        self.setProperty("synced", value)
        self._polish_all()

    def set_pushed(self, value):
        self.setProperty("pushed", value)
        self._polish_all()

    def set_incoming(self, value):
        self.setProperty("incoming", value)
        self._polish_all()

    # 无需 paintEvent — QSS border-left 替代
    # 无需 enterEvent/leaveEvent — QSS :hover 替代
```

### commits.py 批量刷新包裹

```python
def _refresh_formal_boxes(self):
    self._clear_box_layout(self.fm_box_layout)
    self.fm_container.setUpdatesEnabled(False)
    try:
        if not self.session.formal_commits:
            # placeholder...
            if hasattr(self, 'fm_hdr'):
                self.fm_hdr.setVisible(False)
        else:
            if hasattr(self, 'fm_hdr'):
                self.fm_hdr.setVisible(True)
            for i, fc in enumerate(self.session.formal_commits):
                box = FormalCommitBox(i, ...)
                box.set_synced(fc.synced)
                box.set_pushed(fc.pushed)
                if getattr(fc, 'is_incoming', False):
                    box.set_incoming(True)
                # ... connect signals ...
                self.fm_box_layout.addWidget(box)
            self._update_formal_box_styles()
    finally:
        self.fm_container.setUpdatesEnabled(True)
    self.fm_box_layout.addStretch()
    QTimer.singleShot(0, self._refresh_commit_lines)
```

---

## 八、实际实现 vs 设计差异

> 以下记录设计文档中的错误预判和实际实现中的修正。

### 8.1 addSpacing vs setSpacing（设计错误）

**设计文档写的是**：`hdrs.addSpacing(52)` (workshop_tab.py 3.4 节)

**实际**：`addSpacing()` 在 Qt 中是在已有 items 之间**累加**间距，而 `setSpacing()` 是设定**每个 item 之间的统一间隙**。两者语义完全不同。如果在已有 spacing=52 的 layout 中用 `addSpacing(52)`，实际间距会变成 104px。

**最终**：标题行用 `hdrs.setSpacing(52)`，与下方 columns 的 `columns.setSpacing(52)` 一致。

### 8.2 setMinimumWidth vs setFixedWidth（设计错误 — 最关键的根因修复）

**设计文档写的是**：`self.ws_hdr.setMinimumWidth(148)` (3.4 节)

**实际**：`setMinimumWidth(148)` 只设下限不锁定宽度。当 stretch=0 时，QHBoxLayout 按 `sizeHint()` 分配实际宽度。QLabel（ws_hdr, 无 wordWrap）的 sizeHint 是单行文本宽度，而 QWidget 包裹的 layout（ws_column, 内含 wordWrap QLabel）的 sizeHint 更复杂——两者不同 → 标题行和内容行宽度动态偏离。

**根因**：Qt QHBoxLayout 在 stretch=0 时分配规则是 `max(minWidth, min(maxWidth, sizeHint))`。如果 minWidth=148 但 sizeHint > 148，实际分配 > 148。QSS、wordWrap、文本内容都影响 sizeHint。

**最终**：`self.ws_hdr.setFixedWidth(148)` + `self.ws_column.setFixedWidth(148)` 锁死两端宽度。这是 4 轮返工后才找到的真正根因。

### 8.3 QSS 父属性选择器双保险策略（设计过度）

**设计文档建议**（3.1 节）：QSS 用 `QFrame#ws_card[selected="true"] QLabel#ws_summary` + 子 Label 也设 property 的"双保险"策略。

**实际**：测试发现 Qt 6.x 的 QSS 属性选择器（attribute selector）对**子选择器**的支持不稳定——`QFrame#parent[attr] QLabel#child` 这种写法在首次渲染时生效，但在后续 `unpolish/polish` 时不一定重新匹配。更可靠的做法是：

1. **父级 QSS 直接用 property selector**（`QFrame#ws_card[selected="true"]`）控制父级背景/边框 —— 这个稳定
2. **不依赖父属性子选择器改写子 Label 样式**——而是同时给子 Label 设 property + 独立 QSS 规则（如 `QLabel#ws_check[checked="true"]`）
3. **_polish_all() 遍历全部子 widget**——逐个 unpolish/polish，确保每个子 widget 都重新匹配自己的 QSS 规则

设计文档中提到的"双保险"策略被简化了——不再给子 Label 传播父级 property（如 `summary_lbl.setProperty("selected", ...)`），因为子 Label 不需要知道 selected 状态（其文字/背景颜色不变），只有 ws_card 本身需要。

### 8.4 标题行在 Canvas 外 vs 内（架构决策变更）

**设计文档**（3.4 节）：标题行仍在 `workshop_tab.py` 中创建，与 Canvas 分离。

**实际问题**：即使 layout 参数完全一致（spacing=52, setMinimumWidth(148), stretch=0），标题行和 Canvas columns 分处不同 widget 树（一个是 workshop_tab 的直接子 layout，一个是 Canvas 的内部 layout），在 resize 时 QScrollArea 的 viewport 宽度计算与外部 layout 不同步 → 对齐偏差。

**最终决策**：标题行移入 Canvas 内部（`commit_canvas.py` 的 `outer QVBoxLayout`），与 columns 共享同一个 widget 树。这样 Qt 在 resize 时对标题行和内容行同步计算布局，天然对齐。

- workshop_tab.py 不再创建标题行，改为通过 `self.commit_canvas.ws_hdr` / `self.commit_canvas.fm_hdr` 引用并设置文本/可见性

### 8.5 _set_active_formal 删除

**设计文档**（3.5 节）：计划删除。

**实际**：确实删除了。`QGraphicsOpacityEffect` 非选中卡片的设计被放弃——v2 中选中状态仅通过 QSS border-color 变化表示（不改变文字/背景），无需 opacity 效果。

### 8.6 menu_btn 保留 inline stylesheet

**设计文档**（3.2 节）：计划全部 QSS 化。

**实际**：menu_btn (⋯ 按钮) 的样式保留 inline `setStyleSheet()`。原因：该按钮的颜色逻辑与 formal box 状态无关，总是 txt3 (默认) / txt (hover)。如果走全局 QSS 需要定义 `QPushButton#fm_menu_btn`，但 ObjectName 必须是唯一的——多个 box 的 ⋯ 按钮会导致 ObjectName 冲突。用 inline stylesheet 最简单且不会有冲突。

### 8.7 QSS 选择器优先级（Qt 特定行为）

设计文档未充分讨论这一点。实际实现中发现：

- Qt QSS 不支持 CSS specificity 计算。相同 specificity 的选择器，**后定义的覆盖先定义的**。
- 因此 QSS 中 `#fm_card[selected="true"]` 必须写在文件最末尾（在所有 synced/pushed/incoming 之后），否则会被后面的 `[synced]`/`[pushed]` 规则覆盖。
- 背景色规则和边框色规则分离定义也受此限制——必须把高优先级状态的背景+边框规则一起放在文件末尾。

### 8.8 6 步实施的实际顺序

设计文档的计划实施步骤（Step 1→6）在实际中合并为一步完成，因为：
1. 改 QSS（Step 1+2）必须在改 Python 代码（Step 3+4）之前，否则 Python 代码引用的 QSS 选择器不存在
2. 改 Python 代码（Step 3+4）必须在改 commits.py/theme.py（Step 5）之前，否则方法签名不匹配
3. 这些改动互相依赖，分批提交会导致中间状态不可用

实际执行：**QSS 先行 → CommitBox 重写 → Canvas 重写 → workshop_tab/commits/theme 适配**，一次性完成全部 8 个文件的修改。

---

## 九、不与迭代计划冲突

此重构完全落在迭代计划 **F-2（Commit 卡片三层信息结构）** 的范围内。F-2 的目标就是升级 CommitBox 到 v2 卡片设计。本重构在完成 F-2 的同时顺带修复了所有 v0.x 遗留的样式 bug。

实现完成后可以标记 F-2 为 done，然后按迭代计划的顺序继续 F-3。
