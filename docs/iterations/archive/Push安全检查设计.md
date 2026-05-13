# Push 前安全检查 — 设计文档

> 内置硬功能：push 到 GitHub 前自动扫描待推送内容中的敏感信息，命中则阻断并提示用户确认或取消。

---

## 设计目标

- **默认启用** — 用户零配置即获得保护
- **可配置** — 允许自定义扫描规则和忽略模式
- **不误封** — 发现疑似敏感信息时**不直接阻止**，而是列出清单让用户判断
- **与插件体系共存** — 内置扫描器独立于插件系统运行，插件可提供增强/替代实现

---

## 扫描时机

内置在 `core.push_to_backup()` 函数中，在执行 `git push` **之前**调用：

```
push_to_backup(backup_path)
  ├── 1. 安全检查 ← 在此插入
  │     ├── 获取待推送的 commit 内容（git diff HEAD~1..HEAD）
  │     ├── 对 diff 内容进行正则扫描
  │     └── 命中 → 返回警告列表 / 未命中 → 放行
  ├── 2. git push (实际执行)
  └── 3. 返回结果
```

### GUI/CUI 中的交互流程

```
用户点击 Push 按钮
  → core.push_to_backup() 被调用
  → 安全检查开始
     ├─ 安全通过 → 直接 git push（用户无感知）
     └─ 发现敏感信息 → 返回警告列表
        → GUI: 弹 QMessageBox 列表 + "仍然推送" / "取消"
        → CUI: 打印警告列表 + Confirm.ask("仍然推送？")
           ├─ 确认 → 继续 git push（强制推送）
           └─ 取消 → 返回，不执行 push
```

---

## 扫描规则

### 内置规则（默认启用）

基于正则匹配 diff 内容中的敏感模式：

| 规则 ID | 匹配目标 | 正则模式示例 | 严重级别 |
|---|---|---|---|
| `api_key` | 各类 API 密钥硬编码 | `(?:api[_-]?key\|apikey\|api_secret)\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}` | high |
| `password` | 密码赋值 | `password\s*[:=]\s*['\"][^'\"]{4,}` | high |
| `private_key` | 私钥内容块 | `-----BEGIN (RSA \|EC \|OPENSSH )?PRIVATE KEY-----` | critical |
| `token` | 访问令牌 | `(?:access_token\|auth_token\|github_token)\s*[:=]\s*['\"][^'\"]{8,}` | high |
| `aws_key` | AWS Access Key | `AKIA[0-9A-Z]{16}` | critical |
| `github_token_classic` | GitHub 经典 token | `ghp_[A-Za-z0-9]{36}` | high |
| `github_token_fine` | GitHub 细粒度 token | `github_pat_[A-Za-z0-9]{82}` | high |
| `slack_token` | Slack token | `xox[baprs]-[A-Za-z0-9\-]{24,}` | high |
| `generic_secret` | 常见密钥关键词 | `(?:secret\|credential\|passwd\|pwd)\s*[:=]\s*['\"][^'\"]{8,}` | medium |

### 豁免机制

误报在所难免，提供三种豁免方式：

1. **行尾注释豁免** — 在扫描命中的行尾加 `# gitgo-ignore-sensitive` 或 `// gitgo-ignore-sensitive`，该行被跳过
2. **文件级豁免** — 配置 `force_exclude` 中的文件不参与扫描（共享现有的排除规则）
3. **规则级豁免** — 配置文件 `security_scan.ignored_rules: list[str]` 禁用指定规则 ID

### 配置文件扩展

```json
{
  "projects": [{
    "name": "MyApp",
    ...
    "security_scan": {
      "enabled": true,
      "severity_threshold": "medium",
      "ignored_rules": [],
      "extra_patterns": [
        {"id": "myapp_internal", "pattern": "MYAPP_SECRET_[A-Z]+", "severity": "high"}
      ]
    }
  }]
}
```

- `enabled: false` — 完全禁用安全检查
- `severity_threshold` — 只阻塞 ≥ 该级别的命中（`critical` > `high` > `medium` > `low`）
- `extra_patterns` — 用户自定义附加规则

---

## 扫描策略

### 范围控制

安全扫描只检查 **当前待推送的 commit**（即自上次 sync 之后的增量），而非全仓库扫描：

- 获取 `git diff HEAD~1..HEAD`（最近一个正式 commit 的变更）
- 只扫描 **diff 中的新增行**（以 `+` 开头的内容）
- 排除已 `gitgo-ignore-sensitive` 豁免的行
- 排除二进制文件

### 性能

- 扫描在内存中完成，不写临时文件
- 正则预编译为 `re.compile()` 缓存
- 预计扫描耗时 < 200ms（典型 diff 规模）

---

## 与周边模块的关系

| 模块 | 关系 |
|---|---|
| **`core.py`** | `push_to_backup()` 内部调用 `_security_scan(diff_text)`，返回警告列表 |
| **`gui_main.py`** | PushWorker 的 result 信号携带 `security_warnings` 字段，GUI 据此弹警告框 |
| **`cui_main.py`** | `_do_push()` 中检查返回值，有警告则打印列表 + 二次确认 |
| **`config.py`** | `ProjectConfig` 新增 `security_scan` 字段 |
| **`core.py`** | 新增 `DEFAULT_SECURITY_PATTERNS` 常量（内置规则表） |
| **插件系统** | `on_push_start` 钩子可以在安全检查之前/之后插入自定义逻辑 |

---

## 实施步骤

1. 定义内置规则表 `DEFAULT_SECURITY_PATTERNS`（`core.py` 中常量）
2. 实现 `_security_scan(diff_text, config) -> list[Warning]` 扫描函数
3. 在 `push_to_backup()` 中调用扫描，命中时返回警告而非直接抛出
4. 修改 `push_to_backup` 返回值结构：`{"success": bool, "warnings": list, ...}`
5. GUI PushWorker 处理警告列表，弹 QMessageBox
6. CUI `_do_push()` 处理警告列表，二次确认
7. `config.py` 的 `ProjectConfig` 增加 `security_scan` 字段
8. 更新 build.py（无额外依赖，纯 Python 正则，不需修改打包配置）
