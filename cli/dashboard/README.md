# Gitgo Dashboard

Gitgo 的正式终端界面，使用 React、Ink，支持 Bun（默认）和 Node.js。它通过版本化原生协议连接 bundled Native Host；MCP、browser smoke 和 mock renderer 不是正式数据面。

## 开发入口

在本目录执行：

```text
bun install
bun test
bun run build
```

在仓库根目录运行 `run_dashboard_native.bat` 启动完整产品。`dev:trace` 和 `dev:live` 仅用于诊断。

## 贡献要求

- 使用既有输入路由，保持 NormalBar、CommandBar 和场景底栏一致。
- 延续现有颜色、聚焦、间距和状态图标；正文与思考清晰区分。
- 展示真实问题与答案状态，发送回执不能冒充已回答。
- 已回答卡片可收起/展开；流式消息、工具及权限事件不能相互覆盖。
- 调整尺寸、滚动、鼠标复制、输入法、单行与多行粘贴都要检查。
- 界面消费后端状态，不由前端制造完成或权限事实。

## Bun / Node 运行路径

```text
bun run dev
```

最低运行环境：Bun 1.3.0 或 Node.js 22.0.0。构建工具仍需要 Bun；Node 用户运行同一份 `dist/cli.js`，没有另一套 UI 或依赖清单。源码改动后重新执行 `bun run build`。

```text
# 自动探测：优先 Bun，不可用或版本/API 检查失败时选择 Node
node scripts/launch.mjs
# 强制 Node（显式选择失败会报错，不会偷偷切换）
node scripts/launch.mjs --runtime node
# 只显示运行时、版本、探针结果和 TTY 信息
node scripts/launch.mjs --runtime-diagnostics
# 同一套输入测试分别在 Bun 和 Node 下执行
bun run test:input
node --test scripts/runtime.test.mjs
# macOS/Linux 的自动 PTY 回归测试，从仓库根目录执行
python3 scripts/test_dashboard_pty.py
```

`GITGO_DASHBOARD_RUNTIME=auto|bun|node` 设置默认选择，`--runtime` 优先于环境变量；`GITGO_BUN`、`GITGO_NODE` 可指定可执行文件路径。自动探针验证真实运行时身份、最低版本、raw mode API 与分块 UTF-8 解码，不声称检测了终端是否截获物理快捷键。不要把 API 探针成功当作人工终端验收。

macOS、Linux、Windows 原生打包脚本会附带 `dashboard/dist` 与公共启动器。原生命令默认继续使用内置 Bun；执行 `gitgo --runtime node`（Windows 为 `gitgo.exe --runtime node`）切换到本机安装的 Node，并连接包内同一个 Native Host。`--runtime-diagnostics` 可在不启动 Host 的情况下检查版本。旧安装包需要重新构建才有 Node companion bundle。本任务没有把 Node 本身打进发行包。

两条运行路径共用 stdin 解码、Ink 键盘解析、raw mode/粘贴开关、resize 监听、中央 input router、Unicode 宽度和换行实现。收到退出信号时先关闭 renderer、恢复终端模式，再关闭 Host。`Cmd+C/V` 仅在终端将 super 修饰符传给程序时映射为复制/粘贴；macOS Terminal 自己处理的 Cmd 快捷键不会产生应用输入。中文输入法的预编辑也由终端处理，程序消费提交后的 UTF-8 文本。

正式终端验收还需在真实彩色终端分别运行两种环境：中文输入法选词/提交，Ctrl+G、Shift+Enter、Ctrl/Cmd+C/V，含中文和换行的粘贴，缩放窗口，Escape 取消任务，以及退出后恢复正常输入和光标。自动 PTY 注入覆盖协议与生命周期，不能代替物理键盘、IME 和视觉验收。

`dev:trace` 和 `dev:live` 仅用于诊断。它们不能证明正式 Dashboard 的颜色、场景输入、NormalBar/CommandBar、状态栏、流式事件或后端同步正确。

## 修改要求

- 输入统一经过中央 input router，不在组件中堆叠全局按键监听；
- Main Process 与 Subprocess 共用时间线、Markdown、Diff 和决策组件；
- 已提交历史与当前 active cell 分离，最终回答不得覆盖过程轨迹；
- resize 应保持用户滚动锚点；虚拟化不能破坏复制选择；
- 颜色、聚焦、间距和状态图标沿用现有视觉语言；
- UI 只消费 Native Host read model，不自行推导治理真相。

完整贡献要求见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。
可见验收必须在真实彩色终端进行，不能用隐藏 PTY 或传输测试代替。环境中的 `NO_COLOR` 和 `TERM` 会影响颜色。当前结果见 [测试说明](../../docs/VALIDATION.md)，通用贡献规范见 [CONTRIBUTING](../../CONTRIBUTING.md)。
