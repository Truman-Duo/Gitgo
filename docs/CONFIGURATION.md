# 配置

Gitgo 的用户级配置位于 `~/.gitgo/config.json`。在 Dashboard 中保存的设置与直接修改该 JSON 使用同一配置模型；外部修改由运行时重新读取或在下一次启动生效，具体取决于设置类别。

## Launcher

```json
{
  "launcher": {
    "terminal": "auto",
    "command": "",
    "args": []
  }
}
```

`terminal` 支持：

- `auto`：资源管理器启动时优先 Windows Terminal；已有终端则复用当前控制台；
- `current`：始终使用当前控制台；
- `windows_terminal`：使用 Windows Terminal；
- `custom`：使用 `command` 与 `args` 指定的终端。

## Provider

Provider 配置由元数据和凭据两部分组成：

- 元数据：名称、协议、Base URL、模型、上下文窗口、能力探针结果等；
- 凭据：API key 等敏感字段。

Windows 上凭据使用当前用户 DPAPI 加密，单独写入用户级 secret store；明文不会写入项目配置、SQLite、Trace 或 Git。删除 Provider 时应同步清理不再引用的凭据。

支持的协议适配边界包括：

- OpenAI Responses；
- OpenAI Chat Completions；
- Anthropic Messages。

模型原生搜索、Provider 托管搜索和外部 SearXNG 是不同能力。Gitgo 应优先使用已探测到的 Provider 原生能力；SearXNG 是可选后备，不是使用网络搜索的前置条件。

## 项目设置

项目配置保存工作区位置、可选发布/试验仓、提交格式、发布隐私策略和运行时偏好。项目路径可以关联已有目录，不要求位于 Gitgo 源码目录中。

不要在项目配置中保存 API key。不要把项目 SQLite、WAL、Trace、`.env` 或本地 Agent 指令文件提交到远端。
