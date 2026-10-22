# 原生构建与发布要求

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

脚本不会推送、安装、重写 Git 历史或下载构建工具。`-AllowDirty` 只用于本地演练，不能作为正式发布证明。输出目录和 EXE 不进入源码提交。构建输出目录必须为空，每次默认生成新目录，避免旧文件混入。可用 `-Output` 指定 staging、`-ArtifactOutput` 指定 ZIP 与校验文件目录；开发者应使用 `dev-artifacts/` 等开发目录。

流程先核对产品、Dashboard 和 Host 版本，再运行冻结 Host/daemon/工具及历史连续性检查；最后生成带版本的完整 ZIP、SHA256 文件与包内 build-manifest.json。清单记录构建 commit/tree、工作树是否有修改和逐文件摘要；真实 API 与人工终端验收另行记录。ZIP 不会覆盖同名旧包。

构建用 pytest 可由 `-TestPackages` 提供，默认检查开发目录 `dev-artifacts/test-packages`；PyInstaller 使用独立构建依赖，不要求安装进产品运行环境。缺少依赖时明确失败。安装器使用相同产品版本及 Windows 文件版本，需要已安装 Inno Setup。

## macOS package and installer

Unix builds now enforce the Alpha 2 search contract too: supply a standalone
ripgrep >=14 binary and its matching complete third-party notices with
`--ripgrep PATH --ripgrep-notices PATH` (or `GITGO_RIPGREP_PATH` and
`GITGO_RIPGREP_NOTICES`). Both release entry points forward these options.
The engine, notices and integrity manifest are staged beside the frozen Host
and checked by the shared packaged-runtime smoke test. Builds require an empty
output directory and will not overwrite existing archives.

`packaging/build_macos.sh` builds one architecture at a time on macOS. It
compiles the Bun Dashboard as the public `gitgo` command, freezes the Python
Native Host with PyInstaller, performs the packaged Host/Daemon/tool-runner
smoke test, applies ad-hoc code signatures by default, and creates a
`dist-installer/gitgo-macos-<architecture>.tar.gz` archive plus its SHA-256
file. A Developer ID identity can be supplied for a publishable signed build;
Apple notarization remains a separate credentialed release step.

After extracting the archive, run `./install.sh`. The installer is per-user and
does not use `sudo`: payload files go under
`~/Library/Application Support/Gitgo/app`, while `~/.local/bin/gitgo` is an
owned symlink. The installer adds one marked PATH block to the active zsh or
bash profile, or a dedicated fish `conf.d` file. Re-running it performs an
atomic upgrade with rollback.

The Unix installers share a transaction helper included in each package.
An upgrade requires the `.gitgo-install-owner` record, matching product/platform,
current user and canonical install/command directories. Unknown targets (even
empty ones), redirected path components, overlapping source/target directories,
unrelated commands and conflicting PATH blocks are rejected before replacement.
Earlier preview installations without ownership records are not automatically
adopted: keep the old directory and use a new empty destination, or uninstall it
explicitly after preserving needed data. Do not fabricate an ownership marker.

Payload, new command links and the single selected shell profile form one
transaction. Copy/link/profile failures and INT/TERM restore the previous
installation and profile; exclusively created temporary directories avoid
PID-based pre-deletion. Rollback failures retain the recovery transaction and
report its location. A pre-existing install lock is never deleted automatically.
Inspect a stale lock and recovery files before retrying; SIGKILL/power loss cannot
run shell rollback. This is installer safety, not an OS sandbox against hostile
same-user filesystem races. User runtime databases and projects are not touched.

Windows ZIP validation renders the same Windows x86_64 manifest used by the
build, checks bundled Dashboard/Host and Node companion files, and retains the
source commit/tree, per-file hashes and archive checksum. Synthetic archive
regressions do not replace Windows frozen-package acceptance.

Run the installed `uninstall.sh` to remove the application. It removes only
Gitgo-owned command links, marked PATH entries, global configuration, and
provider credentials (including native Keychain items). Runtime databases and
all project directories, repositories, worktrees, and project `.gitgo` data
are preserved. `--keep-config` keeps global configuration and credentials too.

`packaging/release_macos.sh` is the fail-closed release entry point. It mirrors
the Windows privacy, test, clean-tree, commit-message, and build gates and never
pushes or installs anything.

## Linux package and installer

`packaging/build_linux.sh` builds one native architecture at a time on Linux.
The maintained CI target is `linux-x86_64`; the script also accepts native
`aarch64` builders. It compiles the Bun Dashboard, freezes the Python Native
Host with PyInstaller, exercises the packaged Host/Daemon/tool-runner, and
creates `dist-installer/gitgo-linux-<architecture>.tar.gz` together with a
SHA-256 file. Linux executables are always built on Linux rather than
cross-compiled from macOS.

After extracting the archive, run `./install.sh`. The per-user installer does
not use `sudo`: it installs under `${XDG_DATA_HOME:-~/.local/share}/gitgo/app`,
creates owned command links in `~/.local/bin`, and adds one marked PATH block
for bash or zsh (or a fish `conf.d` file). Re-running it is an atomic upgrade
with rollback. The installed `uninstall.sh` removes only Gitgo-owned links,
PATH entries, global configuration, credentials, and application files. XDG
runtime databases and every project/repository remain untouched.

Provider credentials use the desktop Secret Service through `secret-tool` and
are represented on disk only by opaque references. Debian/Ubuntu users can
install the client with `sudo apt install libsecret-tools`; a running Secret
Service such as GNOME Keyring or KWallet is also required. Gitgo fails closed
instead of writing plaintext when the service is unavailable.

`packaging/release_linux.sh` applies the same clean-tree, commit-message,
privacy, Python test, Dashboard test, build, and packaged-runtime gates as the
other platforms. `.github/workflows/linux-package.yml` runs those gates on an
Ubuntu x64 runner, verifies a clean install and safe uninstall, and publishes
the archive as a workflow artifact. CI builds the pinned SQLite source in
`scripts/prepare_linux_sqlite.py` and verifies its official SHA3-256 before
packaging, because common Linux Python distributions may still link an SQLite
version rejected by Gitgo's WAL safety guard.

## 启动与卸载

正常启动使用用户终端偏好；首次配置和以后更改的区别见 [终端说明](../docs/terminal-launcher.md)。安装入口应保留整包组件，用户级 PATH/App Paths 的增删只针对实际安装目录。

卸载不得遍历用户项目、仓库、worktree 或删除运行历史。

## 验收边界

冻结后端的协议 smoke 不证明可见 Dashboard 正常。发布前仍需完整产品在真实终端进行输入、颜色、卡片、流式显示、滚动和复制检查。版本与当前结果见 [版本](../docs/VERSION.md) 和 [测试说明](../docs/VALIDATION.md)。Alpha 2 ZIP 已完成正式终端主流程确认，公开发布分别记录。

该 Alpha 2 验收记录不自动适用于本分支新增的 macOS/Linux 发行包和 Node 运行路径；这些候选仍需重新构建和真实终端验证。
