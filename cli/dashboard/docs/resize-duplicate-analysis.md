# 主屏幕终端 Resize 重复渲染 — 完整问题分析

> 最后更新：2026-08-12（10 次 ANSI 清除尝试 + CUP 实验成功消重但错位 + 3 次错位修复均告失败 + 跨终端验证）

## 一、问题现象

终端缩放时（窗口化↔全屏，或拖动改变尺寸），主屏幕上出现**持续可见的重复渲染**：
旧尺寸渲染留在上方，新尺寸渲染追加在下方，如同两个画面叠在一起。

## 二、环境

| 项目 | 值 |
|------|-----|
| OS | Windows 11 Home China 10.0.26100 |
| 终端 | Windows Terminal v1.22.11141.0 |
| WT_SESSION | 有（触发 `isModernWindowsTerminal()` 分支） |
| Shell | bash (Git Bash) |
| 运行模式 | 主屏幕（`GITGO_ALT_SCREEN` 未设置，`USE_ALT_SCREEN = false`） |
| Ink 渲染 | `vendor/ink` fork（基于 `@anthropic/ink`，已高度定制） |
| BSU/ESU | `SYNC_OUTPUT_SUPPORTED = true`（WT v1.23+ 支持 DEC 2026） |
| ConPTY | 启用（Windows 默认） |

## 三、渲染流水线（关键路径）

```
handleResize()
  → 更新 terminalColumns/terminalRows
  → (alt-screen) resetFramesForAltScreen()
  → (main-screen) CURSOR_HOME + \x1b[J 写入 stdout（清除旧视口，不推 scrollback）
  → (main-screen) 创建空白 frames (screen.height=1), log.reset()
  → (main-screen) needsSkipSync = true
  → 设置 needsEraseBeforePaint (仅 alt-screen)
  → render(currentNode)  // React reconciliation + Yoga layout
    → reconciler.updateContainerSync()  // 同步 commit
    → onComputeLayout()  // Yoga calculateLayout
    → scheduleRender()   // throttle(16ms, leading+trailing)
      → queueMicrotask(onRender)
        → renderer({ frontFrame, backFrame, terminalWidth, terminalRows })
        → log.render(prevFrame, newFrame, altScreen, SYNC_OUTPUT)
          → 判断 resize: height↓ 或 width≠ → fullResetSequence → clearTerminal + fullFrame
          → 否则: 增量 diff（screen.height=1 触发 "growing" 路径）
          → **growing 路径现在用 CUP 绝对定位替代 \n 换行**
          → diffEach 处理 row 0（相对移动），renderFrameSlice 处理 rows 1+（CUP 定位）
          → 每行末尾 emit \x1b[K（EL）清除上一帧同行残留学符
        → optimize(diff)
        → (alt) needsEraseBeforePaint → optimized.unshift(ERASE_THEN_HOME_PATCH)
        → (main) needsEraseBeforePaint 不再设置
        → writeDiffToTerminal(optimized, skipSync)
          → skipSync=true 时不包裹 BSU/ESU
          → terminal.stdout.write(buffer)
```

## 四、关键 ANSI 序列

```
ERASE_SCREEN      = \x1b[2J    // 擦除可见屏幕
ERASE_SCROLLBACK  = \x1b[3J    // 擦除 scrollback（WT v1.22+ 已损坏！）
CURSOR_HOME       = \x1b[H     // 光标移到 (0,0)

clearTerminal     = \x1b[2J\x1b[3J\x1b[H     // fullResetSequence 发出的（已修复，见下）
ERASE_THEN_HOME_PATCH = \x1b[2J\x1b[H       // needsEraseBeforePaint 发出的（无 \x1b[3J）

BSU = \x1b[?2026h    // DEC 2026 同步更新开始
ESU = \x1b[?2026l    // DEC 2026 同步更新结束
```

**重要修复**：`clearTerminal.ts` 已修改，`isModernWindowsTerminal()` 分支不再包含 `\x1b[3J`：
```ts
if (isModernWindowsTerminal()) {
  // \x1b[3J (erase scrollback) is broken on Windows Terminal v1.22+
  // (microsoft/terminal#19086). Emitting it can corrupt the display on resize.
  return ERASE_SCREEN + CURSOR_HOME
}
```

## 五、log-update 的 resize 检测（`log-update.ts:143-148`）

```ts
if (
  next.viewport.height < prev.viewport.height ||         // (A) 高度缩小
  (prev.viewport.width !== 0 &&                          // (B) 宽度变化 + prev 非零
   next.viewport.width !== prev.viewport.width)
) {
  return fullResetSequence_CAUSES_FLICKER(next, 'resize', stylePool)
}
```

| 场景 | (A) height↓ | (B) width≠ | fullReset 触发？ |
|------|------------|------------|-----------------|
| 放大（宽+高都变） | false | true | **是** |
| 缩小（宽+高都变） | true | true | **是** |
| 仅高度增大 | false | false | **否！** |
| 仅高度缩小 | true | false | **是** |

## 六、已知的终端问题

### 6.1 Windows Terminal `\x1b[3J` 损坏（microsoft/terminal#19086）

WT v1.22+ 上 `\x1b[3J`（清除 scrollback）静默无效。已在 `clearTerminal.ts` 中修复，
`isModernWindowsTerminal()` 分支不再 emit `\x1b[3J`。

### 6.2 ConPTY resize 时重放旧 buffer（microsoft/terminal#16911）**【根因】**

**这是 resize 重复渲染的根本原因。** ConPTY（Windows 伪终端）在 resize 时会重放整个
旧终端 buffer。这个重放发生在应用输出**之后**——无论应用发出什么 ANSI 清除序列，
ConPTY 都会在清除后重新注入旧内容。

具体流程（24行→40行全屏 resize）：
1. resize 前：24 行旧内容占据整个视口
2. 终端扩到 40 行，ConPTY 在内部复制旧 buffer
3. 应用 emit `\n`.repeat(40) + `\x1b[H` 滚动+归位
4. 应用 emit 新 40 行内容
5. **ConPTY 重放步骤 2 中缓存的旧 buffer，覆盖步骤 3-4 的输出**
6. 用户看到：旧 24 行内容（重放的）+ 新 40 行内容（部分被覆盖）

没有 ANSI 级别的修复能解决此问题——清除序列在步骤 3 正确执行了，
但步骤 5 的重放发生在步骤 3 之后，覆盖了清除效果。

## 七、历次尝试（8 次，全部失败）

### 调试基础设施

所有尝试都在以下日志基础设施下进行：
- `_debugLog()` 无条件写入 `C:\Users\Duo\.gitgo_resize_trace.log`（appendFileSync）+ stderr
- 日志格式：`[resize] <label> {"ts":<timestamp>, ...}`
- 每个 `onRender` 调用分配递增的 `_debugRid` 用于追踪

### 尝试 1：`emptyFrame(rows, cols)` + `log.reset()` + `displayCursor = null`

**设想**：在 `handleResize` 主屏幕路径中重置 `frontFrame`/`backFrame` 为 `emptyFrame`，
参考 `handleResume` 的模式。`emptyFrame` 创建 0×0 screen 的帧，
期望让 log-update 看到"所有 cell 都是新的"，触发 `fullResetSequence` 发出 `clearTerminal`。

**失败原因**：
`emptyFrame` 的 viewport 是 `{ width: cols, height: rows }`（新尺寸），但 screen 是 `0×0`。

log-update 的 resize 检测中：
- `next.viewport.height < prev.viewport.height` → `rows < rows` → **false**
- `prev.viewport.width !== 0 &&` → `0 !== 0` → **false**（短路！）
- 两个条件都为 false → `fullResetSequence` **永远不会触发**
- 没有 `clearTerminal` → 新内容追加到旧内容之后

### 尝试 2：全尺寸 blank frame + `needsMainScreenClear` + `clearTerminal` diff entry

**设想**：不依赖 `fullResetSequence` 的自动检测。创建**全尺寸空白 screen**
（`createScreen(cols, rows)`），新增 `needsMainScreenClear` 标志，
在 `onRender` 中手动 prepend `{ type: 'clearTerminal', reason: 'resize' }` 到 diff。

**失败原因**：
`{ type: 'clearTerminal' }` 在 `writeDiffToTerminal` 中被转换为 `getClearTerminalSequence()`
= `\x1b[2J\x1b[3J\x1b[H`。其中：
- `\x1b[3J` 在 WT v1.22+ **静默无效**
- `\x1b[2J` 在 WT 上把当前视口内容推到 scrollback 而非销毁
- ConPTY 可能在清除后重放旧 buffer

### 尝试 3（曾为"当前方案"）：复用 `needsEraseBeforePaint` + `ERASE_THEN_HOME_PATCH`

**设想**：不新增 flag，不使用 `clearTerminal`（避开 `\x1b[3J`）。
直接用已有的 `needsEraseBeforePaint` 标志和 `ERASE_THEN_HOME_PATCH`
（`\x1b[2J\x1b[H`，不含损坏的 `\x1b[3J`）。

**实际发出的序列**（完整一帧）：
```
BSU + \x1b[2J\x1b[H + \x1b[2J\x1b[3J\x1b[H + [full_frame_cells] + ESU
     ↑ ERASE_THEN  ↑ fullResetSequence 的 clearTerminal
```

**失败原因**：`\x1b[2J\x1b[H` 在 BSU/ESU 块内不起作用。DEC 2026 同步更新中，
所有输出被缓冲，ESU 时原子应用。但 `\x1b[2J` 在原子应用的上下文中，
旧内容实际上不是被清除，而是与新内容一起被交换。

### 尝试 4：`\x1b[2J\x1b[H` 在 BSU/ESU 块**之外** emit

**设想**：既然 BSU/ESU 内的 `\x1b[2J` 不工作，在 BSU 之前直接 emit 清除序列到 stdout。

**具体改动**：在 `onRender` 中，`needsEraseBeforePaint` 为 true 时，
在 `writeDiffToTerminal` 调用**之前**直接 `stdout.write(ERASE_SCREEN + CURSOR_HOME)`。

**失败原因**：即使清除序列在 BSU/ESU 之外，ConPTY 的重放仍然会覆盖它。

### 尝试 5：`\n`.repeat(rows) 滚动旧内容到 scrollback

**设想**：不使用任何擦除序列。在主屏幕上，每行底部 emit 一个 `\n` 会向上滚动 1 行。
emit `rows` 个 `\n` 后，所有旧内容被推到 scrollback，视口变为空白。
然后 `\x1b[H` 光标归位，新帧写入。

**具体代码**（`handleResize` 中）：
```ts
this.options.stdout.write('\n'.repeat(rows) + CURSOR_HOME);
```

**失败原因**：ConPTY 在 resize 后重放旧 buffer，把刚滚走的旧内容又拉回视口。

### 尝试 6：screen.height=1 触发 log-update "growing" 路径

**设想**：log-update 的 diff 循环中，当 `screen.height < viewport.height` 时，
走 "growing" 路径（`renderFrameSlice`），为每行 emit `\n`。
screen.height=1 强制此路径，避免 `fullResetSequence` 的 `clearTerminal`。

**失败原因**：这只是绕开了 `fullResetSequence`，但 ConPTY 的重放仍然覆盖应用输出。

### 尝试 7：修复 `clearTerminal.ts` 去掉 `\x1b[3J`

**设想**：`isModernWindowsTerminal()` 分支不再 emit `\x1b[3J`（WT v1.22+ 上损坏），
只 emit `\x1b[2J\x1b[H`。

**失败原因**：独立修复有效但不足以解决 resize 重复。`\x1b[3J` 的问题与 resize 重复
是两个独立的问题。

### 尝试 8（当前）：组合方案 — `\n` 滚动 + screen.height=1 + skip BSU/ESU + 修复 clearTerminal

**设想**：同时应用所有可用的修复：
1. `\n`.repeat(rows) 滚动旧内容
2. screen.height=1 强制 growing 路径
3. `needsSkipSync = true` 跳过 BSU/ESU（避免 DEC 2026 双缓冲与 ConPTY 冲突）
4. `clearTerminal.ts` 已修复（无 `\x1b[3J`）
5. 空白 frame + `log.reset()` + `prevFrameContaminated = true`
6. `needsEraseBeforePaint` 仅对 alt-screen 设置

**当前代码状态（`ink.tsx` `handleResize`）**：
```ts
// Main screen: scroll old content into scrollback via \n, then reset frames
if (!this.altScreenActive && !this.isPaused && this.options.stdout.isTTY) {
  this.options.stdout.write('\n'.repeat(rows) + CURSOR_HOME);
  const blankMain = (): Frame => ({
    screen: createScreen(cols, 1, this.stylePool, this.charPool, this.hyperlinkPool),
    viewport: { width: cols, height: rows },
    cursor: { x: 0, y: 0, visible: true },
  });
  this.frontFrame = blankMain();
  this.backFrame = blankMain();
  this.log.reset();
  this.displayCursor = null;
  this.prevFrameContaminated = true;
  this.needsSkipSync = true;
}
// needsEraseBeforePaint only for alt screen
if (!this.isPaused && this.options.stdout.isTTY) {
  this.needsEraseBeforePaint = this.altScreenActive;
}
```

**调试日志确认的事实**：
- `handleResize main-screen scroll+reset` 正确触发
- `onRender#N skipSync` 正确 emit（`needsSkipSync` 被消费）
- 每次 resize 只有**一个** `onRender` 调用（无双重渲染）
- `needsErase` = `false`（主屏幕不再使用擦除）
- `firstClear` = `-1`（没有任何帧包含 clearTerminal）
- 补丁数合理（719 或 999，正常完整帧）

**仍然失败**。日志证明应用层面一切正确——单个干净帧、无双重渲染、无冲突序列。
问题在应用层面之下。

### 尝试 9：Reset scroll region + origin mode（用户建议）

**设想**：resize 后 terminal 可能保留旧的 scroll region（DECSTBM）和 origin mode（DECOM）。
如果 scroll region 仍是旧尺寸（1-24），`\n` 只滚动 24 行而不是全部 40 行。
如果 origin mode 开启，`\x1b[H` 跳到 scroll region top 而非屏幕 (0,0)。

**具体代码**（`handleResize` 主屏幕分支，先 reset modes 再做滚动）：
```ts
this.options.stdout.write(
  '\x1b[?6l' +              // DEC origin mode off
  RESET_SCROLL_REGION +     // \x1b[r — reset scroll region to full screen
  ERASE_SCREEN +            // \x1b[2J
  CURSOR_HOME +             // \x1b[H
  '\n'.repeat(rows) +       // scroll
  CURSOR_HOME,              // back to (0,0)
);
```

**失败原因**：日志确认 `modeReset+scroll` 正确执行，但视觉上仍然重复。
scroll region / origin mode 不是根因。

### 尝试 10：Main-screen resize 时临时借用 alt-screen（用户建议的方向 E）

**设想**：在 `handleResize` 主屏幕分支中，先 `\x1b[?1049h` 进入 alt buffer，
再 `\x1b[?1049l` 退出，测试进出 alt-screen 是否能重置 ConPTY 的异常状态。

**具体代码**（`handleResize` 主屏幕分支）：
```ts
this.options.stdout.write(
  '\x1b[?1049h' +            // enter alt buffer (saves main screen)
  '\x1b[2J\x1b[H' +          // clear alt buffer
  '\x1b[?1049l' +            // exit alt (RESTORES saved main screen!)
  '\x1b[2J\x1b[H' +          // clear restored main content
  '\n'.repeat(rows) +        // scroll any residual
  CURSOR_HOME,
);
```

**现象变更糟了**：不只是全屏切换会重复，**任何尺寸变化（包括纯水平拖拽）都触发重复渲染**。

**关键洞察**：`\x1b[?1049l` 退出 alt-screen 时**恢复进入前的主屏幕内容**。
快速拖拽 resize 时：
```
第1次：enter alt(保存状态A) → exit alt(恢复A) → 清除 → render
第2次：enter alt(保存状态B，含第1次render) → exit alt(恢复B) → 清除 → render
第3次：...
```
每次 `\x1b[?1049l` 都往 scrollback 里塞了一帧旧内容。多次 resize 叠加，
scrollback 堆积多层，终端 reflow 时全显示出来。

**这说明两个核心事实**：
1. **清除序列本身是有效的**——如果清除不工作，换成 alt-toggle 不会变**更**糟。
2. **问题机制是 scrollback 堆积，不是"清除不够干净"**——每次 resize 都在 scrollback
   里增加一帧，旧帧从未真正被删除（`\x1b[2J` 只推到 scrollback，`\x1b[3J` 在 WT 上损坏）。

## 八、跨终端验证（2026-08-12）

三种终端各自独立运行 `bun run src/main.tsx --mock`，均复现 resize 重复渲染：

| 终端 | 渲染前端 | 底层 | 复现？ |
|------|---------|------|--------|
| Windows Terminal v1.22 | WT 自有引擎 | ConPTY | **是** |
| Git Bash 独立窗口（mintty） | mintty 自有引擎 | ConPTY | **是** |
| VS Code 集成终端 | xterm.js | ConPTY | **是** |

三个不同渲染前端都复现，共同点只有 **ConPTY**。排除了"WT 前端 reflow bug"假说。

### 最小复现（纯 Node）失败的原因

Bun 在 Windows ConPTY 下 `process.stdout.on('resize')` 不触发，且 `stdout.columns`/`rows`
在 resize 后不更新。即使加 `setRawMode(true)` 也无效。Ink 的 `handleResize` 正常工作的原因
尚未确定（可能与 React reconciler、事件循环模式有关）。纯 ANSI 复现不可行，
因为无法可靠检测到 resize 事件。

## 九、根因分析：两阶段理解

### 第一阶段：ConPTY scrollback 堆积（实验 1-10 → 部分正确）

**10 次 ANSI 级别尝试全部失败 + 3 种终端跨验证后，确认为 ConPTY 的 scrollback 堆积问题。**

核心机制：
1. `\x1b[2J`（erase screen）在 Windows 终端上不是真删除——内容被推到 scrollback
2. `\x1b[3J`（erase scrollback）在 WT v1.22+ / ConPTY 上损坏，静默无效
3. `\n` 滚动同样只是把内容推进 scrollback
4. Resize 时，ConPTY 对 scrollback 历史做 reflow，把旧帧重新纳入可视区域
5. 无论应用怎么清屏、怎么滚，旧帧从未被真正销毁——只是被推进了 scrollback 这个"地下仓库"，resize 时又被翻出来

### 第二阶段：CUP 实验的意外发现（实验 11-14 → 根因更复杂）

**实验 11**（条件 CUP + 无清除）是唯一成功消除重复渲染的版本。用户确认：
"没有了，虽然错位但是没有重复渲染了"。这直接证明了 `\n` → scrollback 堆积 → resize reflow 的因果链。

但**实验 14**（仅把条件 CUP 改为无条件 CUP，无其他改动）让重复渲染复现。这一发现挑战了
"纯 scrollback 堆积"假说：

- CUP 无论在条件还是无条件形式下，都不产生 scrollback 条目
- 如果根因是 scrollback 堆积，那么无条件 CUP 也应该消除重复
- 但无条件 CUP 却让重复回来了

**可能的解释**：
1. **输出时序/竞态**：条件 CUP 跳过某些行（因 `screen.cursor.y < y` 不满足），导致输出体积减小、
   写入更快，可能在 ConPTY 的 resize-reflow 之前完成。无条件 CUP 输出更多字节，
   写入时间更长，可能在 ConPTY reflow 之后才到达，产生竞争。
2. **错位掩盖**：条件 CUP 的错位可能恰好把新内容写到了旧内容的"空隙"中，
   视觉上不表现为叠放。无条件 CUP 把内容放在正确位置，反而与 ConPTY reflow 的旧内容
   重叠，表现为重复。
3. **ConPTY 的 resize 事件本身会重放 buffer**：无论应用输出什么，ConPTY 在 resize 时
   都可能重放当前 viewport 内容（而非 scrollback）。CUP 不解决这个问题。

### 待验证的关键问题

1. ConPTY resize 时到底重放的是什么——scrollback 还是当前 viewport？
2. 实验 11 的条件 CUP 为什么恰好避开了重放？
3. CUP + 清除序列的组合为什么反而不如单独的（条件）CUP？
4. Claude Code 主仓库是如何修复此问题的？

**14 次尝试的排除矩阵**：

| # | 方案 | BSU/ESU | 清除方式 | 帧重置 | 结果 |
|---|------|---------|---------|--------|------|
| 1 | emptyFrame + log.reset | 有 | fullResetSequence 的 clearTerminal | 0×0 screen | ❌ fullReset 不触发 |
| 2 | blank frame + needsMainScreenClear | 有 | clearTerminal (\x1b[3J损坏) | 全尺寸 screen | ❌ \x1b[3J 无效 |
| 3 | needsEraseBeforePaint (alt+main) | 有 | ERASE_THEN_HOME_PATCH | 无 | ❌ BSU内\x1b[2J不工作 |
| 4 | needsEraseBeforePaint outside BSU | 无(清除) | ERASE_THEN_HOME_PATCH 直接写 | 无 | ❌ ConPTY 重放覆盖 |
| 5 | \n.repeat 滚动 | 有 | 无(滚动替代) | 全尺寸 screen | ❌ ConPTY 重放覆盖 |
| 6 | screen.height=1 growing 路径 | 有 | 无(growing \n) | screen=1 | ❌ ConPTY 重放覆盖 |
| 7 | 修复 clearTerminal.ts | 有 | 取决于调用路径 | 取决于调用路径 | ❌ 独立问题 |
| 8 | 组合方案 | **跳过** | \n滚动 + screen=1 | screen=1 + reset | ❌ ConPTY 重放覆盖 |
| 9 | scroll region + origin mode reset | 跳过 | \x1b[?6l+\x1b[r+\x1b[2J+\n滚动 | screen=1 + reset | ❌ 非 terminal mode 问题 |
| 10 | alt-screen 临时切换 | 跳过 | alt enter→clear→exit→clear→\n | screen=1 + reset | ❌ **变更糟** |
| **11** | **CUP 替代 \n（条件）** | **跳过** | **无清除序列** | screen=1 + reset | **✅ 消重！但错位** |
| 12 | 无条件CUP + EL + DECAWM + CURSOR_HOME | 跳过 | CURSOR_HOME | screen=1 + reset | ❌ 重复复现 |
| 13 | 无条件CUP + EL + CURSOR_HOME+\x1b[J | 跳过 | CURSOR_HOME+\x1b[J | screen=1 + reset | ❌ 重复复现 |
| 14 | 仅无条件CUP（回退到实验11+修复条件） | 跳过 | 无清除序列 | screen=1 + reset | ❌ 重复复现 |

### 证据链

1. 应用 emit 正确的清除 + 渲染序列（日志确认单个干净帧）
2. 清除序列在 BSU/ESU 内不工作 → ConPTY 在应用输出后重放
3. 清除序列在 BSU/ESU 外也不工作 → ConPTY 重放发生在所有应用输出之后
4. `\n` 滚动方式也不工作 → ConPTY 重放覆盖滚动效果
5. 跳过 BSU/ESU 也不工作 → 与 DEC 2026 双缓冲无关
6. scroll region / origin mode reset 不工作 → 不是 terminal mode 问题
7. alt-toggle 让情况更糟 → 确认机制是 scrollback 堆积（alt exit 恢复旧内容 = 加速堆积）
8. 三种终端（WT / mintty / xterm.js）都复现 → 不是渲染前端问题
9. Claude Code 主仓库已修复此问题 → 说明存在 workaround
10. **CUP（实验11）消除重复** → `\n` → scrollback 堆积是必要条件
11. **无条件CUP（实验14）重复复现** → scrollback 堆积不是充分条件；输出时序或错位掩盖效应也在起作用

### 尝试 11（**关键突破**）：CUP 绝对定位替代所有 `\n` 输出

**设想**：如果 scrollback 堆积的根源是 `\n`（linefeed），那么把所有 `\n` 替换为
CUP（Cursor Position, `\x1b[{row};{col}H`）绝对光标定位，从源头阻止 scrollback 产生。

**具体改动**（`log-update.ts` 三处 `\n` 发射点全部替换）：

1. **行间前进**（原 `\n`×N 滚动到目标行）：改为 CUP 定位到 `(y+1, 1)`：
   ```ts
   if (screen.cursor.y < y) {
     screen.txn(prev => {
       const cupSeq = cursorPosition(y + 1, 1)
       return [[{ type: 'stdout', content: cupSeq }], { dx: -prev.x, dy: y - prev.y }]
     })
   }
   ```

2. **行尾换行**（原 CR+LF）：改为纯虚拟光标推进，不 emit 任何序列：
   ```ts
   screen.txn(prev => [[], { dx: -prev.x, dy: 1 }])
   ```

3. **光标归位**（原 CR+LF×N 滚动到底部）：改为 CUP 定位：
   ```ts
   const cupSeq = cursorPosition(next.cursor.y + 1, next.cursor.x + 1)
   return [[{ type: 'stdout', content: cupSeq }], { dx: ..., dy: ... }]
   ```

**`handleResize` 主屏幕分支**：不做任何 stdout 写入，仅重置 frame：
```ts
const blankMain = (): Frame => ({
  screen: createScreen(cols, 1, ...),
  viewport: { width: cols, height: rows },
  cursor: { x: 0, y: 0, visible: true },
});
this.frontFrame = blankMain();
this.backFrame = blankMain();
this.log.reset();
this.prevFrameContaminated = true;
this.needsSkipSync = true;
```

**结果：✅ 重复渲染消失！但显示错位。**

这是 **14 次尝试中唯一成功消除重复渲染的一次**。用户确认："没有了，虽然错位但是没有重复渲染了"。

**错位原因分析**：
1. **条件 CUP**：`if (screen.cursor.y < y)` 只在第一行触发。后续行的光标位置不匹配，
   内容被写到错误的行上。
2. **无行尾清除**：旧帧的长行残留学符未被清除。新行的短内容之后，旧内容的尾部仍然可见。

**关键意义**：这直接证明了 `\n` → scrollback 堆积 → ConPTY resize reflow →
重复渲染的因果链。CUP 不产生 scrollback 条目，所以 ConPTY 在 resize 时没有
历史内容可 reflow——重复渲染消失。

### 尝试 12：无条件 CUP + EL + DECAWM + CURSOR_HOME

**设想**：在实验 11 的基础上修复错位：
1. 无条件 CUP（每行都发射，修复合条件遗漏）
2. EL `\x1b[K`（每行末尾擦除，清除旧帧残留学符）
3. DECAWM `\x1b[?7l`/`\x1b[?7h`（禁用/启用自动换行，防止全宽行写入触发隐式 `\n`）
4. `CURSOR_HOME`（在 handleResize 中 emit `\x1b[H`，锚定终端光标到 (0,0)）

**结果：❌ 重复渲染复现！** 但与之前不同——"上一个的页面残留，新的渲染在最底下"
（pre-resize 页面残留在上方，新渲染在下方）。

**失败原因分析**：
1. DECAWM_OFF 在 `renderFrameSlice` 内部发射，但 row 0 由 `diffEach` 循环处理，
   在 `renderFrameSlice` **之前**执行——row 0 不受 DECAWM 保护。
2. `CURSOR_HOME` 仅移动光标到 (0,0)，不擦除旧视口内容。`diffEach` 循环对 row 0
   使用 `cursorMove(0,0)`（空字符串），终端光标在 (0,0)，第一个 cell 写入正确位置。
   但不隐式清除旧内容——旧帧的残余字符留在原位。
3. DECAWM mode change 可能本身与 ConPTY 产生意外交互（类似 BSU/ESU 在实验 3 中的行为）。

### 尝试 13：回退 DECAWM，改用 `\x1b[H\x1b[J` 清除旧视口

**设想**：去掉 DECAWM（最可疑的新增项），用 `\x1b[J`（Erase from cursor to End of Display）
配合 `\x1b[H` 在 handleResize 中彻底清除旧视口：
```ts
this.options.stdout.write(CURSOR_HOME + '\x1b[J');
```
`\x1b[J` 从光标位置擦到屏幕末尾，与 `\x1b[2J` 不同，不会把内容推到 scrollback。

保留：无条件 CUP + EL。

**结果：❌ 仍然重复渲染。** 与尝试 12 表现相同——旧内容在上，新内容在下。

**关键发现**：即使清除序列不推 scrollback，即使 CUP 不产生 scrollback，
重复渲染依然存在。这说明问题可能**不完全**是 `\n` 造成的 scrollback 堆积。

### 尝试 14：回退到纯实验 11 状态 + 仅修复条件 CUP

**设想**：实验 11 是唯一成功消除重复的版本。既然尝试 12-13 的"修复"反而导致重复复现，
那么只修复实验 11 最明显的错位 bug（条件 CUP），不加任何其他改动。

即：无条件 CUP，但不加 EL、不加 DECAWM、不加任何 handleResize 清除序列。

**结果：❌ 重复渲染复现。**

**这是最重要的发现。** 仅仅把条件 CUP 改为无条件 CUP（去掉 `if`），就从
"无重复但错位"变成了"有重复"。这意味着：

- **实验 11 的条件 CUP 有一些意外副作用恰好阻止了重复渲染**
- 这不太可能是 scrollback 机制（CUP 不产生 scrollback 无论条件还是无条件）
- 可能是输出时序或缓冲区大小触发了 ConPTY 的不同行为路径
- 或者条件 CUP 造成的错位本身"掩盖"了重复（内容被写到错误位置，视觉上不显示为叠放）

## 十、ConPTY scrollback 堆积详解

### 为什么 scrollback 无限增长？

Ink 在主屏幕的每帧渲染都使用 `\n`（linefeed）来移动光标位置：
- log-update diff 中 `\n` 在每行末尾滚动 1 行
- `screen.height=1` 的 growing 路径每行 emit `\n`
- `\n`.repeat(rows) 滚动旧内容

每次 `\n` 在终端底部执行时，整个屏幕向上滚动 1 行——顶部的 1 行内容被推进 scrollback。

**正常操作的后果**：Ink 每渲染一帧（16ms~50ms 间隔），就往 scrollback 塞几行到几十行。
运行几分钟后，scrollback 可能已有数千行历史。

`\x1b[2J`（erase screen）不销毁内容——它把当前视口内容也推进 scrollback，然后显示空白。
`\x1b[3J`（erase scrollback）本应清空 scrollback，但在 ConPTY/WT v1.22+ 上**静默无效**。

**scrollback 永远不会被清空**。旧帧永远留在缓冲区里。

### resize 时的行为

1. 终端 resize → ConPTY 收到新尺寸
2. ConPTY 对 scrollback 历史做 reflow（按新宽度重新折行）
3. Reflow 后 viewport 锚定可能算错——旧帧内容被重新纳入可视区域
4. 应用 emit 清除序列 + 新内容
5. 但步骤 2-3 发生在 render 完成之后，旧内容已经出现在可视区域上方
6. 用户看到：旧帧（scrollback reflow 出来的）+ 新帧（应用刚写的）

### 为什么只有主屏幕有这个问题？

Alt-screen 是一个独立的 buffer，没有 scrollback。进入 alt-screen 时，
ConPTY 不保留主屏幕的 scrollback 历史。退出 alt-screen 时，恢复进入前的主屏幕。
Alt-screen 内部的渲染不会往主屏幕的 scrollback 塞内容。

## 十一、可能的解决方向

### 方向 A：延迟重绘（事后擦除）

Resize 后等 150-200ms，ConPTY reflow 完成后，再做一次清除+重绘覆盖旧内容。

```
handleResize → render → [用户看到重复 150ms] → ERASE + re-render
```

**优点**：改动小，不依赖终端特性
**缺点**：resize 后会有可见闪烁（旧内容闪现 150ms 再消失）

### 方向 B：PTY wrapper 层拦截

在应用和 ConPTY 之间插入 PTY wrapper，拦截 resize 事件，主动管理 buffer。
Claude Code 可能用了类似方案（`claudefix`，未开源）。
需要 C/C++/Zig 层面实现，工作量较大。

### 方向 C：强制使用 alt-screen

`GITGO_ALT_SCREEN=1` 切换到 alt-screen 模式，彻底避开 scrollback。
**已知可用**。代价是退出时不保留主屏幕历史，且不能边看之前的命令输出边操作 dashboard。

### 方向 D：消除 `\n` 输出（从源头阻止 scrollback 堆积）**【实验 11-14 已测试】**

改动 log-update 的输出格式，用绝对光标定位（CUP, `\x1b[{row};{col}H`）
替代 `\n` 换行。每行直接写到固定位置，不产生 scrollback 积累。

**实验 11（条件 CUP）**：✅ 消除重复，但显示错位
**实验 12-13（无条件 CUP + EL + 清除）**：❌ 重复复现
**实验 14（仅无条件 CUP）**：❌ 重复复现

**结论**：CUP 消除 `\n` 是必要的，但单独不够。条件 CUP 的成功可能是意外副作用（错位掩盖、
输出时序）而非真正的修复。需要理解条件 vs 无条件 CUP 的行为差异才能推进此方向。

### 方向 E：resize 后主动清空 scrollback

找到在 ConPTY 上能真正清空 scrollback 的方法：
- 临时切 alt-screen 再切回（但已证实 `\x1b[?1049l` 会恢复旧内容）
- 某些终端支持的专有序列
- `\x1b[3J` 的替代方案

目前 `\x1b[3J` 在 WT v1.22+ 上确认无效，暂无已知替代序列。

### 方向 F：上报并等待上游修复

向 microsoft/terminal 提 issue，附上 scrollback 堆积的证据和分析。
但修复周期不可控，且需要用户更新终端版本。

### 方向 G：研究实验 11 的条件 CUP 为何成功

**这是当前最有价值的方向。** 实验 11 是唯一成功的版本。需要精确理解：
1. 条件 CUP `if (screen.cursor.y < y)` 的完整行为——哪些行被跳过？跳过的行在终端上如何显示？
2. 被跳过的行是否产生了"意外的正确效果"（如恰好覆盖了 ConPTY 重放的旧内容）
3. 能否通过模拟条件 CUP 的终端输出模式来复现成功，同时修复错位？

这需要逐帧对比实验 11（条件 CUP）和实验 14（无条件 CUP）的终端输出序列差异。

## 十二、当前代码状态（实验 13 残留）

### `vendor/ink/src/core/clearTerminal.ts`
- `isModernWindowsTerminal()` 分支不再包含 `\x1b[3J`
- 返回 `ERASE_SCREEN + CURSOR_HOME` 替代 `ERASE_SCREEN + ERASE_SCROLLBACK + CURSOR_HOME`

### `vendor/ink/src/core/termio/csi.ts`
- 新增 `ERASE_TO_END_OF_LINE = csi('K')` 常量（EL，行尾擦除）

### `vendor/ink/src/core/log-update.ts`
- **三处 `\n` 替换为 CUP**（行间前进、行尾换行、光标归位）
- **无条件 CUP**：每行开头必定发射 `cursorPosition(y+1, 1)`
- **EL**：每行末尾发射 `\x1b[K` 清除上一帧残留学符
- 导入：`cursorPosition`, `ERASE_TO_END_OF_LINE`

### `vendor/ink/src/core/ink.tsx`
- 新增 `needsSkipSync` 字段：主屏幕 resize 帧跳过 BSU/ESU
- 新增 `needsExitAltAfterRender` 字段（实验 10 遗留，未使用）
- 新增 `_debugLog()` 调试函数，无条件写入 trace 文件 + stderr
- 导入新增：`RESET_SCROLL_REGION`
- `handleResize` 当前状态：
  ```ts
  // Main screen: Clear old viewport without pushing to scrollback.
  // CSI H = home (0,0). CSI J = erase cursor to end of display.
  if (!this.altScreenActive && !this.isPaused && this.options.stdout.isTTY) {
    this.options.stdout.write(CURSOR_HOME + '\x1b[J');
    const blankMain = (): Frame => ({
      screen: createScreen(cols, 1, this.stylePool, this.charPool, this.hyperlinkPool),
      viewport: { width: cols, height: rows },
      cursor: { x: 0, y: 0, visible: true },
    });
    this.frontFrame = blankMain();
    this.backFrame = blankMain();
    this.log.reset();
    this.displayCursor = null;
    this.prevFrameContaminated = true;
    this.needsSkipSync = true;
  }
  ```
- `onRender`：主屏幕 block 有意留空
- `writeDiffToTerminal` 使用 `skipSync` 参数

**注意**：当前代码（实验 13 状态）仍有重复渲染。已知实验 11（条件 CUP，无清除序列）
是唯一成功消除重复的版本，但该版本有显示错位。条件 CUP vs 无条件 CUP 的行为差异
是理解根因的关键线索，尚未充分解释。

## 十三、调试方法

### Trace 日志位置
- 文件：`C:\Users\Duo\.gitgo_resize_trace.log`
- Stderr：同时输出到 stderr（通过 `.bat` 的 `2>` 重定向捕获）

### 启动命令
```batch
C:\Users\Duo\.bun\bun.exe run C:\...\src\main.tsx --mock
```

### 关键日志解读
```
[resize] handleResize {"prevCols":80,"prevRows":24,"cols":120,"rows":40,"alt":false}
[resize] handleResize main-screen scroll+reset {"cols":120,"rows":40}
[resize] onRender#5 write {"hasDiff":true,"patches":719,"types":["stdout","stdout",...],"firstClear":-1}
[resize] onRender#5 skipSync {}
```
- `alt:false` = 主屏幕
- `firstClear:-1` = 没有 clearTerminal
- `skipSync` = 跳过了 BSU/ESU
- 单个 `onRender#N` 调用 = 无双重渲染

## 十四、最终决策（2026-08-12）

### 结论：ConPTY 层面问题，ANSI 无解

经过 14 次实验（ANSI 清除 ×10 + CUP ×4），确认根因为 ConPTY `ResizePseudoConsole`
在 resize 时 reflow scrollback 历史到视口，发生在所有应用 ANSI 输出之后。
应用层无法阻止、无法清除、无法预测。

### Claude Code 对比

检查了 Claude Code 的 Ink fork（`C:\Users\Duo\Desktop\claude-code-main\claude-code-main\packages\@ant\ink\src\core\`）：

| 方面 | Claude Code | gitgo (改后) |
|------|-------------|-------------|
| 默认模式 | Alt-screen（强制，无退出） | Windows 默认 alt-screen |
| 主屏幕 TUI | 不支持 | 支持（非 Windows 默认，`GITGO_ALT_SCREEN=0` 退出） |
| `handleResize` 主屏幕分支 | 无 | 无（已回退实验改动，交给 alt-screen） |
| `log-update.ts` | `\n` 换行（与 Ink 上游相同） | `\n`（已回退 CUP 实验） |
| `clearTerminal.ts` `\x1b[3J` | 仍包含（无影响，因为 alt-screen 下不调用） | 已移除 Windows Terminal 分支的 `\x1b[3J` |
| 开关 | 无 | `GITGO_ALT_SCREEN=0/1` 显式开关 |

Claude Code 从未尝试解决主屏幕 resize 问题——它直接禁用了主屏幕 TUI。
gitgo 保留主屏幕支持（非 Windows 平台 + Windows 手动退出），并保留显式开关和完整注释。

### 三种方案记录

| 方案 | 说明 | 状态 |
|------|------|------|
| A. 延迟重绘 debounce | resize 后延迟重绘，等 ConPTY reflow 完 | 未采用（时机不可靠） |
| B. `PSEUDOCONSOLE_RESIZE_QUIRK` | 由 PTY host 设置 flag 禁用 reflow | 不可行（应用层无法设置） |
| C. Alt-Screen 强制 | Windows 默认 alt-screen | **已采用** |

### 改动清单

1. **`cli/dashboard/src/main.tsx`** — `USE_ALT_SCREEN` 改为 Windows 默认 true，保留 `GITGO_ALT_SCREEN=0` 退出
2. **`cli/dashboard/vendor/ink/src/core/log-update.ts`** — 回退 CUP/EL 实验改动，恢复 `\n` 换行
3. **`cli/dashboard/vendor/ink/src/core/ink.tsx`** — 回退 handleResize 主屏幕分支 + debug 日志 + `needsSkipSync`
4. **`docs/VERSION.md`** — 添加已知问题条目（根因 + 三种方案 + 决策）
5. **`cli/dashboard/docs/resize-duplicate-analysis.md`** — 本文档（最终更新）

### 未来可能方向

如果未来有终端模拟器支持 `PSEUDOCONSOLE_RESIZE_QUIRK` 作为 opt-in，可以通过环境变量检测并自动走主屏幕。
或者在 Windows Terminal 修复 `\x1b[3J` 后，主屏幕 `\x1b[3J` 清除 scrollback 可能减轻问题（但不能根治——ConPTY reflow 不依赖 scrollback 内容是否被标记清除）。
最彻底的长期方案是 Ink 上游将主屏幕渲染从 `\n` 改为 CUP 绝对定位——但这需要 Ink 上游接受并维护。当前 Claude Code / gitgo 的 alt-screen 方案足够。
