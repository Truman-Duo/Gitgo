# Dashboard CLI 技术栈对照分析

> 对照对象：`claude-code-main`（Claude Code reverse-engineered 版本）
> 日期：2026-06-11
> 目的：找出 gitgo dashboard 为什么反复出 bug，以及可以参考的技术方案

---

## 一、当前 gitgo dashboard 技术栈

```
Python + Rich (Live/Panel/Layout) + msvcrt (Windows keyboard)
```

**Rich Live 的工作方式**：

1. `Live(screen=True)` → 进入交替缓冲区（alternate buffer）→ 全屏渲染
2. `live.update()` → 把整个 UI tree（Layout/Panel/Table）重新渲染到交替缓冲区 → flush 整屏到 stdout
3. `refresh_per_second=4` → Rich 内部定时器每 250ms 自动调 `_build()` + 渲染
4. 每次渲染 = 把几百行 ANSI 转义序列一次性写入终端

**msvcrt 的工作方式**：

1. `msvcrt.kbhit()` → 轮询 Windows 控制台输入缓冲区
2. `msvcrt.getch()` → 阻塞读取一个字节
3. 手动解析字节序列识别 ↑↓←→ Enter Esc 等

**BUG 根因**：Rich 的 stdout flush（几百行 ANSI）和 msvcrt 的 stdin read（逐个字节）在**同一个 Windows 终端**上执行时产生竞争。Windows 控制台 API 不是线程安全的，Rich 写 stdout 期间 msvcrt 读 stdin 会阻塞等待，反之亦然。`:` 按下后 `cmd_buf=""` 触发 dirty→render→stdout flush，如果此时用户敲下一个字符，msvcrt.getch() 和 Rich 的 ANSI 写入对撞 = 程序冻结。

---

## 二、Claude Code（Ink）的技术栈

```
TypeScript/React + @ant/ink (React for terminals) + Node.js process.stdin
```

**Ink 的工作方式**：

1. React reconciler diff 虚拟 DOM → **只重写变化的行**（log-update.ts 逐行 diff）
2. `process.stdin.setRawMode(true)` → 终端进入 raw mode（不缓冲、不回显、不处理 Ctrl+C）
3. `process.stdin.on('readable', handler)` → 非阻塞事件驱动读取，不轮询
4. **输入和输出解耦**：stdin 事件驱动（Node.js event loop），stdout 写由 React 协调（只在组件状态变化时触发）

**Ink 的核心优势**：

| 维度 | Rich Live | Ink |
|------|-----------|-----|
| 渲染方式 | 整屏重写（full re-render） | 逐行 diff（只写变化） |
| 键盘输入 | msvcrt 轮询/阻塞 | Node.js readable event 非阻塞 |
| stdin/stdout 关系 | **同一个线程竞争** | event loop 调度，自然解耦 |
| 转义序列解析 | 无（手动 if/else） | 状态机 tokenizer（termio/tokenize.ts） |
| 布局引擎 | Rich 自己的布局 | Yoga layout（Facebook FlexBox 引擎） |
| 交替缓冲区 | Rich 的 AlternateScreen | 自定义 AlternateScreen 组件 |

---

## 三、可以借鉴的具体实现

### 3.1 stdin 输入：用原始 tty 替代 msvcrt

**Claude Code 的做法**（`packages/@ant/ink/src/components/App.tsx`）：

```typescript
// 1. 设 raw mode — 终端不缓冲、不回显
process.stdin.setRawMode(true)
process.stdin.setEncoding('utf8')

// 2. 事件驱动读 — 非阻塞，有数据时回调
process.stdin.on('data', (chunk) => {
  // chunk 是字符串，可能包含多字节（如粘贴文本）
  // 也可能是转义序列（如 ↑ = \x1b[A）
  tokenizer.feed(chunk) // → tokens
  parser.parse(tokens)  // → key events
})

// 3. 退出时恢复
process.stdin.setRawMode(false)
```

**gitgo 可采用的 Python 等价方案**：

Python 标准库 `tty` + `termios` 在 Unix 可用，但在 Windows 不可用。Windows 替代：

```python
import sys
import msvcrt

# 方案 A：保持 msvcrt 但改成非阻塞读
# msvcrt 不支持非阻塞，所以这个路线不行

# 方案 B：使用 Windows Console API (kernel32)
import ctypes
kernel32 = ctypes.windll.kernel32
# GetStdHandle(STD_INPUT_HANDLE=-10) → SetConsoleMode → ReadConsoleInput

# 方案 C（推荐）：用 select 模式
# 但 select 不支持 Windows 控制台 handle...
```

**结论**：Windows 上用纯 Python 做非阻塞终端 I/O 极其困难。msvcrt 是唯一的选择，但它的竞争问题无法解决。**最现实的方案是放弃交替缓冲区全屏渲染**，改用**增量输出**模式——每 5 秒刷新时只 print 增量行，不依赖 Rich Live 的重渲染管线。

### 3.2 渲染：放弃 Rich Live，改用增量输出

**Claude Code 的 Ink 渲染**（`packages/@ant/ink/src/core/log-update.ts`）：

```typescript
// 虚拟 DOM diff → 只重写变化的行
// 第 N 次渲染：比较 output[N] vs output[N-1]
// 如果第 3 行变了 → 光标移到第 3 行 → 擦除 → 写新内容
// 其他行不动
```

**gitgo 可采用的最简方案**：不用任何渲染框架，直接 stdout 写。

```python
# 不进入交替缓冲区，不依赖 Rich Live
# 每次刷新：
#   1. print 分隔线
#   2. print 当前数据（表格/详情）
#   3. print 底部指令行
# 只有一个 screen 位置——纯追加模式
```

如果仍然需要"固定位置刷新"，可以考虑一个**独立的键盘线程**：

```python
import threading
import queue

# 线程 A：键盘监听线程（纯 stdin，无 stdout 写入）
# 线程 B：主渲染线程（纯 stdout，无 stdin 读取）
# 通过 queue 通信
```

这是彻底解决 stdin/stdout 竞争的唯一架构方案。Claude Code 因为 Node.js event loop 天然就是单线程事件驱动（无竞争），Python 需要显式分线程。

### 3.3 键盘解析：状态机替代 if/else

**Claude Code 的 termio/tokenize.ts**（`packages/@ant/ink/src/core/termio/tokenize.ts`）：

一个完整的 VT100 状态机，识别：
- 普通字符 → text token
- `\x1b[A` → CSI cursor up → 转换为 key event
- `\x1b[1;5A` → Ctrl+Up → modifier-aware
- `\x1b[200~...\x1b[201~` → 粘贴 bracketed paste
- `\x1b[<0;10;20M` → 鼠标点击

**gitgo 当前的 `_getch()` 只处理了最基本的箭头键**，不支持：
- 组合键（Ctrl+Up, Shift+Enter...）
- 粘贴（bracketed paste 会把多字符一次性读入）
- 鼠标事件
- 终端响应序列（kitty query、focus-in/out）

**可考虑的最简改进**：保持 msvcrt 但引入一个 buffered reader：

```python
def _read_input():
    """从一个独立线程中持续读 stdin，存到队列"""
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            # 把字节 append 到 buffer
            # 用状态机识别完整 tokens
            # 完整 token → queue
```

这样即使 Rich 在写 stdout，输入也不丢失（msvcrt 在独立线程中）。

### 3.4 交替缓冲区：可选的轻量方案

**Claude Code 的 AlternateScreen**（`packages/@ant/ink/src/components/AlternateScreen.tsx`）：

```typescript
// 进入时：输出 \x1b[?1049h（保存当前屏幕，切换到备用屏幕）
// 退出时：输出 \x1b[?1049l（恢复原始屏幕）
```

如果 gitgo dashboard 放弃 Rich Live 但保留交替缓冲区，可以手动写：

```python
# 进入
sys.stdout.write('\x1b[?1049h')
sys.stdout.flush()

# 渲染期间 —— 直接用 print 或 cursor movement
# 光标移到某行某列: '\x1b[{row};{col}H'
# 擦除到行尾: '\x1b[K'
# 擦除到屏幕尾: '\x1b[J'

# 退出
sys.stdout.write('\x1b[?1049l')
sys.stdout.flush()
```

这样不需要 Rich 的重量级封装，也就不会有 Rich 内部的自动刷新竞争。

---

## 四、推荐方案：三种架构选择

### 方案 A — 纯增量模式（最稳，最易实现）

```python
# 独占 pattern: 每次刷新追加新行，不清屏，不交替缓冲区
while True:
    print('\n' + '=' * 60)
    print_table(data)
    print('Commands: q quit ...')
    wait_for_key()  # 阻塞，但只有键盘在动
    # 无 stdout 竞争
```

**优点**：零竞争，msvcrt 直接可用
**缺点**：终端会累积输出，不美观

### 方案 B — 双线程模式（中复杂度，类似 Ink 架构）

```python
import threading, queue
key_queue = queue.Queue()

def keyboard_thread():
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            # 解析为事件
            key_queue.put(event)

def render_thread():
    # 进入交替缓冲区
    # 主循环：
    #   1. 检查 key_queue → 更新状态
    #   2. 重渲染（写 ANSI 到 stdout）
    #   3. sleep(0.05) 或 wait on event
```

**优点**：stdin/stdout 物理分离，无竞争
**缺点**：需要手动管理 ANSI 光标和屏幕擦除

### 方案 C — 保持 Rich Live 但修 stdin 竞争（最小改动）

不改渲染引擎，只修 stdin 部分：把 `_kbhit()/_getch()` 移到独立线程，键盘事件通过 `queue.Queue` 传给主循环。Rich Live 的渲染仍然在主线程。

**核心改动**：在 `cmd_dashboard` 开头启动一个 `KeyboardThread`，它持续读 msvcrt 并把 key 事件 put 进队列。主循环的 `_kbhit()/_getch()` 替换为 `queue.get(timeout=0.05)`。键盘线程不写 stdout，主线程不读 stdin → 无竞争。

这是对现有代码改动最小的方案。

---

## 五、对 gitgo dashboard 的改造建议

优先推荐 **方案 C**（双线程）。原因是：

1. Rich Live 的渲染质量是好的——问题只在 stdin/stdout 竞争
2. 键盘线程是 ~20 行新增代码
3. 不改变现有的 `_build()` / `_view_overview` / `_view_detail` 逻辑

**关键实现要点**（避免 CC 反复出错）：

1. **键盘线程必须是 daemon 线程**，主线程退出时自动终止
2. **键盘线程不 import Rich**，不写 stdout
3. **queue 用 `queue.Queue(maxsize=100)`**，满时不阻塞（用 `put_nowait`）
4. **Rich Live 的 `refresh_per_second` 必须删掉或设为 0**，只有脏状态才触发渲染
5. **`_build()` 是纯函数**，不修改任何外部状态（当前已经做到了）
