# Gitgo Dashboard

正式终端界面使用 React、Ink 和 Bun，通过原生协议连接 Native Host。浏览器诊断页、简化 renderer 和 MCP 不替代产品界面。

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

可见验收必须在真实彩色终端进行，不能用隐藏 PTY 或传输测试代替。环境中的 `NO_COLOR` 和 `TERM` 会影响颜色。当前结果见 [测试说明](../../docs/VALIDATION.md)，通用贡献规范见 [CONTRIBUTING](../../CONTRIBUTING.md)。
