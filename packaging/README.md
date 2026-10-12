# Windows 构建与发布要求

Gitgo 的 Windows 产品包包含 Dashboard 与内部 Native Host，必须一起构建、校验和分发。源码合入不等于二进制已经发布。

## 构建环境

需要 Bun 和通过 Gitgo SQLite WAL 安全检查的 Python。运行时由 `GITGO_PYTHON` 或用户级 Gitgo 安装位置选择；脚本不自动改用不安全系统 Python。PyInstaller 可以由 `-PyInstallerPackages` 指定构建依赖目录。安装器还需要已安装的 Inno Setup。

搜索资产需要独立 ripgrep >=14 及匹配的完整第三方许可；可用 `-Ripgrep`、`-RipgrepNotices` 指定。终端验证参考资产通过既有构建参数提供，缺失不能用本机 PATH 掩盖。

## 统一验证入口

```text
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1 -BuildInstaller
```

分别进行验证、验证后生成 staging、验证后生成 staging 和安装器。流程包括工作树/提交格式、运行时安全、被跟踪内容扫描、后端/前端测试、构建及冻结后端 smoke。既有扫描未覆盖全部隐私引用，发布候选仍需人工核对。

脚本不会推送、安装、重写 Git 历史或下载构建工具。`-AllowDirty` 只用于本地演练，不能作为正式发布证明。输出目录和 EXE 不进入源码提交。构建输出目录必须为空，每次默认生成新目录，避免旧文件混入。

流程先核对产品、Dashboard 和 Host 版本，再运行冻结 Host/daemon/工具及历史连续性检查；最后生成带版本的完整 ZIP、SHA256 文件与包内 build-manifest.json。清单记录构建 commit/tree、工作树是否有修改和逐文件摘要；真实 API 与人工终端验收另行记录。ZIP 不会覆盖同名旧包。

构建用 pytest 可由 `-TestPackages` 提供，默认检查本地 `.gitgo/dev-test-deps`；PyInstaller 使用独立构建依赖，不要求安装进产品运行环境。缺少依赖时明确失败。安装器使用相同产品版本及 Windows 文件版本，需要已安装 Inno Setup。

## 启动与卸载

正常启动使用用户终端偏好；首次配置和以后更改的区别见 [终端说明](../docs/terminal-launcher.md)。安装入口应保留整包组件，用户级 PATH/App Paths 的增删只针对实际安装目录。

卸载不得遍历用户项目、仓库、worktree 或删除运行历史。

## 验收边界

冻结后端的协议 smoke 不证明可见 Dashboard 正常。发布前仍需完整产品在真实终端进行输入、颜色、卡片、流式显示、滚动和复制检查。版本与当前结果见 [版本](../docs/VERSION.md) 和 [测试说明](../docs/VALIDATION.md)。Alpha 2 ZIP 已完成正式终端主流程确认，公开发布分别记录。
