# Gitgo Dashboard

Gitgo 的正式终端界面，使用 React、Ink 和 Bun。它通过版本化原生协议连接 bundled Native Host；MCP、browser smoke 和 mock renderer 不是正式数据面。

## 开发

```text
bun install
bun test
bun run build
```

从仓库根目录启动完整产品：

```text
run_dashboard_native.bat
```

直接运行开发入口：

```text
bun run dev
```

`dev:trace` 和 `dev:live` 仅用于诊断。它们不能证明正式 Dashboard 的颜色、场景输入、NormalBar/CommandBar、状态栏、流式事件或后端同步正确。

## 修改要求

- 输入统一经过中央 input router，不在组件中堆叠全局按键监听；
- Main Process 与 Subprocess 共用时间线、Markdown、Diff 和决策组件；
- 已提交历史与当前 active cell 分离，最终回答不得覆盖过程轨迹；
- resize 应保持用户滚动锚点；虚拟化不能破坏复制选择；
- 颜色、聚焦、间距和状态图标沿用现有视觉语言；
- UI 只消费 Native Host read model，不自行推导治理真相。

完整贡献要求见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。
