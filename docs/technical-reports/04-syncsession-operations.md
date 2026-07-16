# 报告四：SyncSession 状态机与操作层深度解析

> gitgo v0.35 | 2026-07-16 | 完全透底技术报告

---

## 概述

SyncSession 是 gitgo 的**运行时状态机**——编排 scan → formalize → sync → push 全流程。它是 daemon 和 CLI 背后的共享引擎，所有文件操作和 git 操作都通过它统一管理。

Operations 层提供纯函数（文件扫描、git 操作、安全检查），Steps 层将 Operations 包装为与 SyncSession 零耦合的纯函数管线，Adapters 层抽象文件系统和 git 操作（支持 Local/SSH/SMB 三种实现）。

**核心文件**：

| 文件 | 行数 | 职责 |
|------|------|------|
| `sync_session.py` | 1317 | 状态机：24 个 step_* 方法 + 语义层 |
| `operations/scan.py` | 253 | 文件扫描 + 对比 + 排除 |
| `operations/git.py` | 228 | Git log 解析 + commit template |
| `operations/sync.py` | 207 | 文件同步 + 推送到远程 |
| `operations/security.py` | 121 | 9 种敏感信息正则扫描 |
| `operations/diff.py` | 80 | 提交变更摘要 |
| `operations/models.py` | 24 | FileEntry + CommitInfo 数据模型 |
| `operations/utils.py` | 107 | 哈希/二进制检测/glob 匹配 |
| `steps/scan.py` | 62 | scan + incremental scan 纯函数 |
| `steps/commits.py` | 76 | load commits + create formal 纯函数 |
| `steps/sync.py` | 47 | sync + push 纯函数 |
| `adapters/file_adapter.py` | 102 | FileAdapter ABC |
| `adapters/git_runner.py` | 86 | GitRunner ABC |
| `adapters/local_file_adapter.py` | 77 | 本地文件系统实现 |
| `adapters/local_git_runner.py` | 109 | 本地 git 实现 |
| `adapters/ssh_file_adapter.py` | 216 | SSH SFTP 实现 |
| `adapters/ssh_git_runner.py` | 180 | SSH git 实现 |
| `adapters/smb_file_adapter.py` | 108 | SMB UNC 路径实现 |
| `adapters/factory.py` | 59 | 适配器工厂 |
| `remote/github.py` | 113 | GitHub REST API |
| `remote/gitlab.py` | 113 | GitLab REST API |

---

## 一、SyncSession 状态机

### 1.1 完整状态转换图

```
IDLE
  ├─ step_check_trial()      → TRIAL_CHECKING
  ├─ step_scan()             → SCANNING
  ├─ step_load_commits()     → (内部状态，不改变 stage)
  └─ (从文件加载)            → 恢复之前状态

SCANNING
  └─ 扫描完成               → IDLE / SELECTING

SELECTING
  ├─ step_toggle_workspace_selection() → SELECTING (保持)
  └─ step_create_formal_commit()       → COMMITTING

COMMITTING
  ├─ step_sync()             → SYNCING
  └─ step_delete_formal()    → IDLE

SYNCING
  ├─ step_push()             → PUSHING
  └─ (失败)                  → FAILED

PUSHING
  └─ (完成)                  → IDLE

TRIAL_CHECKING
  ├─ step_triage_incoming()  → TRIAL_REVIEWING
  └─ (无新提交)              → IDLE

TRIAL_REVIEWING
  └─ step_triage_incoming()  → IDLE / INCOMING_CONFIRMING

INCOMING_CONFIRMING
  ├─ step_confirm_accept()   → IDLE
  └─ step_cancel_accept()    → TRIAL_REVIEWING

FAILED
  └─ (可重试)               → (恢复到之前状态)
```

### 1.2 语义层 `_build_semantic_layer()`

SyncSession 的 `status_dict()` 除了返回原始数据，还计算一个"语义层"——将原始计数推导为 Agent 可消费的布尔决策：

```python
def _build_semantic_layer(self, ...):
    return {
        "workspace_entropy": len(entries) / max_changed_threshold,
        "trial_requires_review": len(trial_incoming) > 0,
        "safe_to_formalize": workspace_entropy < 0.5 and not has_integrity_warnings,
        "safe_to_publish": safe_to_formalize and not has_drift,
        "blocked_reason": "integrity" if has_integrity_warnings
                     else "drift" if has_drift
                     else "entropy" if workspace_entropy >= 0.8
                     else None,
        "suggested_next_action": "formalize" if safe_to_formalize
                            else "sync" if has_formal
                            else "push" if has_synced
                            else "scan",
        "action_queue": [...],  # 按优先级排队
    }
```

---

## 二、主线流程完整追踪

### 2.1 scan 流程

```
step_scan(hash_cache)
  │
  ├─ scan_workspace(workspace, exclude_patterns, ws_adapter)
  │   ├─ ws_adapter.walk(root) → 所有文件路径
  │   ├─ _is_excluded() → 过滤排除项（.gitignore + force_exclude）
  │   └─ 返回 FileEntry[]（状态标记为 "unknown"）
  │
  ├─ compare_files(workspace, backup, file_list, ..., hash_cache)
  │   ├─ for each file:
  │   │   ├─ hash_cache.lookup(rel_path, mtime, size)
  │   │   │   └─ 命中 → 使用缓存的 SHA256
  │   │   │   └─ 未命中 → _hash_file() 计算 SHA256
  │   │   ├─ ws_hash vs bk_hash 对比
  │   │   └─ 状态: new | modified | same | renamed
  │   └─ 返回 FileEntry[]（含状态）
  │
  └─ _run_integrity_checks(entries, workspace_path, project)
      ├─ _detect_mass_override()
      ├─ _detect_identity_file_deletion()
      └─ _detect_structure_collapse()
```

### 2.2 formalize 流程

```
step_load_commits()
  │
  └─ get_git_log(repo_path, since_hash, git_runner)
      ├─ git log --format="%H||%s||%b" → 解析
      ├─ 解析 subject: "type(scope): description" 或 "type: description"
      └─ → CommitInfo[] (hash, subject, type, scope, body)

step_create_formal_commit(selected_indices, message, template_name)
  │
  ├─ 从 selected_indices 选取 commits
  ├─ build_commit_template(commits, project, template_name, git_runner)
  │   ├─ TemplateManager.get_template(template_name)
  │   ├─ 变量替换: {prefix}, {number}, {type}, {scope}, {subject}
  │   └─ → 最终 commit message
  ├─ _find_next_number(backup_path, prefix, git_runner)
  │   ├─ 读取 .gitgo/next_number 本地计数器
  │   ├─ git log release repo → 取 max 编号
  │   └─ → 下一个编号
  └─ → FormalCommit (存入 session.formal_commits)
```

### 2.3 sync 流程（Gate A）

```
step_sync(formal_index)
  │
  ├─ 【Gate A】合约漂移检测
  │   ├─ contract = ContractManager.load(workspace)
  │   ├─ detect_drift(workspace, changed_files, contract)
  │   ├─ check_feature_signatures(workspace, changed_files, contract)
  │   └─ 有漂移 → 返回失败 + 告警列表
  │
  ├─ 【Gate A】依赖图检查
  │   ├─ dep_graph = load_dep_graph(workspace)
  │   └─ get_dependents(workspace, changed_files) → 受影响文件
  │
  ├─ 【Gate A】Memory Snapshot
  │   └─ snapshot_tool_memories(workspace, backup, project)
  │
  ├─ 【Gate A】Contract 更新
  │   └─ ContractManager.update_feature(...)
  │
  ├─ 【Gate A】Lesson Harvest
  │   └─ harvest_lessons(workspace, project_name, tech_stack)
  │
  └─ sync_to_backup(entries, commit_message, workspace, backup, ..., adapters)
      ├─ for each FileEntry (status != "same"):
      │   └─ ws_adapter.copy_within(src, backup_dst)
      ├─ bk_git_runner.add_all()
      ├─ bk_git_runner.commit(message)
      └─ → True / False
```

### 2.4 push 流程（Gate B）

```
step_push(skip_scan=False)
  │
  ├─ 获取待推送 diff（如果不跳过扫描）
  │
  ├─ 【Gate B】隐私扫描
  │   ├─ _security_scan(backup_path, config, git_runner)
  │   │   ├─ _get_push_diff() → diff 文本
  │   │   ├─ for each pattern in DEFAULT_SECURITY_PATTERNS:
  │   │   │   └─ re.findall(pattern, diff_lines)
  │   │   ├─ 应用 severity_threshold 过滤
  │   │   ├─ 检查 gitgo-ignore-sensitive 注释
  │   │   └─ 去重
  │   └─ 有敏感信息 → 返回失败 + 告警列表
  │
  ├─ 【Gate B】Authorship 清洗（可选）
  │   ├─ strip_authorship_from_message(message)
  │   └─ strip_authorship_from_code(content, aggressive)
  │
  └─ push_to_backup(backup, remote, ...)
      ├─ bk_git_runner.push(remote)
      └─ → (success, alerts[])
```

---

## 三、Trial 流程

### 3.1 检查 + 三叉决策

```
step_check_trial()
  └─ get_trial_log(trial_path, since_hash, git_runner)
      → IncomingChange[] (hash, message, author, timestamp, body)

step_triage_incoming(index, action)
  ├─ ACCEPT:
  │   ├─ bk_git_runner.fetch(trial_remote)
  │   └─ bk_git_runner.cherry_pick(incoming.hash)
  │       ├─ 成功 → cherry-pick 到 release repo
  │       └─ 冲突 → 返回失败，提示手动处理
  │
  ├─ PROMOTE:
  │   ├─ ws_git_runner.fetch(trial_remote)
  │   ├─ ws_git_runner.checkout -b incoming/{hash}
  │   └─ → 创建 incoming/* 分支继续开发
  │
  └─ DISCARD:
      └─ _record_processed(hash, "discarded")
```

---

## 四、Operations 操作层详解

### 4.1 scan_workspace() 遍历算法

```python
def scan_workspace(workspace, exclude_patterns, file_adapter):
    files = []
    for entry in file_adapter.walk(workspace):
        rel_path = _normalize_path(entry.path)
        if _is_excluded(rel_path, exclude_patterns):
            continue
        if file_adapter.is_file(entry.path):
            files.append(rel_path)
    return sorted(files)
```

### 4.2 compare_files() 对比算法

```python
def compare_files(workspace, backup, file_list, progress_callback,
                  ws_adapter, bk_adapter, normalize_eol=True, hash_cache=None):
    results = []
    for rel_path in file_list:
        ws_stat = ws_adapter.stat(ws_path)
        bk_stat = bk_adapter.stat(bk_path) if exists else None

        # 1. 快速路径：hash_cache 命中
        if hash_cache:
            ws_hash = hash_cache.lookup(rel_path, ws_stat.mtime, ws_stat.size)
            if ws_hash:
                bk_hash = hash_cache.lookup(rel_path + "@backup", ...)
                if bk_hash:
                    status = "same" if ws_hash == bk_hash else "modified"
                    results.append(FileEntry(rel_path, status, ws_hash, bk_hash))
                    continue

        # 2. 慢速路径：计算哈希
        ws_hash = ws_adapter.hash_file(ws_path, normalize_eol)
        bk_hash = bk_adapter.hash_file(bk_path, normalize_eol) if bk_stat else None

        # 3. 判定状态
        if bk_stat is None:
            status = "new"
        elif ws_hash != bk_hash:
            status = "modified"
        else:
            status = "same"

        # 4. 更新缓存
        if hash_cache:
            hash_cache.store(rel_path, ws_stat.mtime, ws_stat.size, ws_hash)

        results.append(FileEntry(rel_path, status, ws_hash, bk_hash))

    return results
```

### 4.3 EOL 归一化

`_hash_file(filepath, normalize_eol=True)` 在计算 SHA256 前将 `\r\n` 替换为 `\n`，避免 Windows/Linux 换行符差异导致误报。

### 4.4 _find_next_number() 双源确定

```python
def _find_next_number(backup_path, prefix, git_runner, workspace_path):
    # 1. 读取本地计数器
    local_counter = 0
    counter_file = Path(workspace_path) / ".gitgo" / "next_number"
    if counter_file.exists():
        local_counter = int(counter_file.read_text().strip())

    # 2. 从 release repo git log 提取最大编号
    log_output = git_runner.log(backup_path, format="%s")
    git_max = 0
    for line in log_output.splitlines():
        match = re.match(rf"\[{prefix}-(\d+)\]", line)
        if match:
            git_max = max(git_max, int(match.group(1)))

    # 3. 取 max + 1
    next_num = max(local_counter, git_max) + 1

    # 4. 更新本地计数器
    counter_file.write_text(str(next_num))

    return next_num
```

**为什么需要双源**：本地计数器防止重复编号（同一编号被 sync 多次），git log 扫描防止计数器丢失后的编号冲突。

### 4.5 安全扫描（security.py）

9 种内置敏感信息正则：
```
aws_key, private_key, github_token, github_fine_token,
slack_token, api_key, token, password, generic_secret
```

扫描逻辑：
- 只扫描**新增行**（diff 中的 `+` 行）
- 支持 severity_threshold 过滤
- 支持 `gitgo-ignore-sensitive` 注释标记豁免
- 去重（同一文件同一行不同模式只报告一次）

---

## 五、Adapters 适配器层

### 5.1 设计模式

```
FileAdapter (ABC)          GitRunner (ABC)
 ├── LocalFileAdapter       ├── LocalGitRunner
 ├── SSHFileAdapter         └── SSHGitRunner
 └── SMBFileAdapter

create_adapters_for_node(node: RepoNode) → (FileAdapter, GitRunner)
```

工厂函数根据 `RepoNode.file_access.kind` 创建适配器对：
- `LOCAL` → `(LocalFileAdapter, LocalGitRunner)`
- `SSH` → `(SSHFileAdapter, SSHGitRunner)`
- `SMB` → `(SMBFileAdapter, LocalGitRunner)`

### 5.2 FileAdapter ABC

13 个抽象方法：`exists`, `is_file`, `is_dir`, `is_symlink`, `walk`, `read_bytes`, `read_text`, `write_bytes`, `write_text`, `mkdir`, `hash_file`, `is_binary`, `copy_within`, `stat`

### 5.3 GitRunner ABC

10 个抽象方法：`run`, `add_all`, `commit`, `rev_parse`, `log`, `push`, `diff`, `fetch`, `cherry_pick`, `is_git_repo`

### 5.4 SSH 适配器的延迟连接

```python
class SSHFileAdapter(FileAdapter):
    def __init__(self, host, port, username, key_path, root):
        self._client = None  # 延迟初始化
        ...

    def _ensure_connected(self):
        if self._client is not None:
            return
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(self._host, port=self._port,
                            username=self._username, key_filename=self._key_path)
        self._sftp = self._client.open_sftp()
```

### 5.5 LocalGitRunner 的 Windows 兼容

```python
class LocalGitRunner(GitRunner):
    def run(self, args, capture_output=True, timeout=30, ...):
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        return subprocess.run(
            ["git", "-C", self._repo_path] + args,
            capture_output=capture_output, text=True,
            creationflags=creationflags, timeout=timeout,
        )
```

---

## 六、Remote 远程层

### 6.1 RemoteConnector ABC

```python
class RemoteConnector(ABC):
    def __init__(self, target: RemoteTarget, token: str): ...
    @abstractmethod
    def is_configured(self) -> bool: ...
    @abstractmethod
    def create_release(self, tag, name, body) -> dict: ...
    @abstractmethod
    def get_repo_info(self) -> dict: ...
    @abstractmethod
    def list_issues(self, state="open") -> list[dict]: ...
    @abstractmethod
    def create_pr(self, title, body, head, base) -> dict: ...
```

### 6.2 GitHubConnector 实现

使用 `httpx` 库调用 GitHub REST API（`api.github.com`）。`_parse_owner_repo()` 从 URL 解析 owner/repo。

### 6.3 GitLabConnector 实现

使用 `httpx` 库调用 GitLab REST API v4（`gitlab.com/api/v4`）。`create_pr()` 映射为 GitLab Merge Request。`_parse_project_path()` 处理 URL 编码的项目路径（`/` → `%2F`）。

### 6.4 工厂函数

```python
def create_connector(target, token):
    if target is None:
        return None
    if target.kind == "github":
        return GitHubConnector(target, token or os.environ.get("GITHUB_TOKEN"))
    elif target.kind == "gitlab":
        return GitLabConnector(target, token or os.environ.get("GITLAB_TOKEN"))
    return None
```

---

## 七、Config 系统

### 7.1 ProjectConfig 数据模型

```python
@dataclass
class ProjectConfig:
    name: str
    note: str = ""
    workspace: RepoNode    # 工作区
    release: RepoNode      # 正式仓库
    trial: Optional[RepoNode] = None  # 外部试验仓库
    commit_format: dict = {...}
    force_exclude: list[str] = [...]
    security_scan: dict = {...}
    integrity: dict = {...}
    authorship: dict = {...}
```

向后兼容属性（旧扁平格式 → RepoNode 格式）：
- `workspace_path` → `self.workspace.file_access.path`
- `backup_path` → `self.release.file_access.path`
- `sync_base` → `self.release.file_access.path`
- `trial_path` → `self.trial.file_access.path if self.trial else None`

### 7.2 ConfigManager 配置发现路径

优先级：exe 目录 > CWD > `~/.vernier/` > 包目录

---

## 八、测试覆盖

| 测试文件 | 测试内容 | 测试方法 |
|----------|----------|----------|
| `test_sync_session.py` | 状态机 stage、trial 方法、session 持久化 | 集成 |
| `test_operations.py` | get_trial_log 返回 IncomingChange | 集成（真实 git） |
| `test_local_file_adapter.py` | 14 个方法完整测试 | 集成（真实文件系统） |
| `test_local_git_runner.py` | git 操作完整测试 | 集成（真实 git） |
| `test_ssh_adapters.py` | SSH 适配器全 mock | 单元（Mock paramiko） |
| `test_smb_adapter.py` | UNC 路径构建 + 工厂集成 | 单元 |
| `test_remote.py` | URL 解析 + API 调用 mock | 单元 |
| `test_factory.py` | 适配器工厂分发 | 单元 |
| `test_models.py` | 数据模型字段 | 单元 |
| `test_config.py` | Config 加载/保存/迁移 | 单元+集成 |
| `test_diff.py` | get_diff_summary | 集成（真实 git） |
| `test_template_manager.py` | CommitTemplate CRUD | 单元 |
| `test_regression.py` | 回归 bug 修复验证 | 单元 |

---

## 九、已知限制与潜在问题

1. **SyncSession 1317 行仍然很大**：虽然 v0.29 将纯函数提取到 steps/ 和 operations/，但 SyncSession 本身作为编排层仍然承载了太多职责（状态管理 + 语义计算 + session 持久化 + 决策钩子）。

2. **Gate A/B 检查硬编码在 step_* 中**：无法在 gate 和 step 之间插入自定义检查——所有检查逻辑在 `step_sync()` 和 `step_push()` 中顺序执行。

3. **legacy 格式迁移仍在 config.py 中**：`from_dict()` 包含自动迁移逻辑。迁移完成后可以移除。

4. **SSH 适配器未在生产环境充分测试**：SSH 测试全 mock，没有真实 SSH 环境验证。

5. **SMBFileAdapter 只有文件操作没有 SMBGitRunner**：SMB 节点使用 LocalGitRunner——即 SMB 文件操作通过 UNC 路径，但 git 操作需要在本地执行（因为 git 需要文件系统锁）。

6. **compare_files 的 O(n) 哈希计算**：大项目的全量扫描可能很慢。hash_cache 缓解了这个问题，但首次扫描仍然需要计算所有文件的 SHA256。

---

## 十、设计审查总结

### ✅ 已实现
- 24 个 step_* 方法覆盖完整生命周期
- Gate A（sync）+ Gate B（push）双重保护
- Trial 三叉决策（accept/promote/discard）
- 语义层自动推导
- Session 持久化到 .gitgo/session.json
- 三实现适配器（Local/SSH/SMB）
- GitHub + GitLab 远程 API
- 向后兼容的旧格式自动迁移

### ⚠️ 部分实现
- SSH 适配器未在真实环境测试
- Gate A/B 检查不可扩展
- 大项目全量扫描性能

### ❌ 未实现
- SyncSession 的增量 sync（当前每次 sync 都是全量文件复制）
- 远程仓库 Webhook 集成
- Sync 冲突的自动解决策略
