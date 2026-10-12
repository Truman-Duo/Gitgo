# 可重复的原生包构建与验收

Actions 的 **Native Unix packages** 是维护者入口，Windows 上的浏览器也可触发。
Run workflow 选择含该工作流的分支，ref 填完整 commit SHA 或维护者控制的不变 tag。
CLI：`gh workflow run linux-package.yml --ref <workflow-branch> -f ref=<commit-or-tag>`。
工作流须先存在于仓库默认分支才出现在手动运行列表；合入前通过 PR 的 Gitgo CI
调用相同 reusable workflow 验证，不为此向主线直接推送。fork 首次运行仍可能需要维护者批准。

ref 只解析一次，两个平台和独立验收 worker 都 checkout 解析所得的完整 SHA。
PR CI 构建的是 GitHub 测试合并 commit，不应把它混同贡献分支 head。
成功运行的 Artifacts 有 `native-<target>-<commit>` 与 `acceptance-<target>-<commit>`；
下载前确认完整 SHA，解压 artifact ZIP 后才是发行 tar.gz 和校验文件。
`gh run download <run-id> -n native-<target>-<commit>` 也可下载。不自动创建公开 Release。

## 当前支持声明

自动候选目标为 macOS 15 ARM64 和 Ubuntu 24.04 x86_64（glibc 2.39 基线）。
本机脚本中的 macOS Intel/Linux ARM 是实验能力，**没有声明已支持或已验收**。
不承诺旧 macOS、旧 glibc、musl 发行版。每个新架构须增加独立原生 build/accept matrix，
不能只改文件名，也不能用跨编译结果替代原生执行。runner 标签固定但系统镜像会由
GitHub 更新；这不是逐字节可复现或离线构建声明。

Python 3.14.7、PyInstaller 6.22.3、Bun 1.4.2 固定；Python 全部依赖使用
`packaging/requirements-build.lock` 的精确版本与 PyPI 官方资产 SHA-256。
Bun frozen lockfile、Actions 完整 SHA、SQLite 3.53.4 官方 SHA3-256 和
ripgrep 15.2.0 官方 SHA-256/完整通知固定。Python/Bun 安装资产由固定 setup
action 自身验证；不是自建签名供应链证明。APT 系统编译工具由 runner/Ubuntu 管理，
尚未锁到不可变系统镜像。修改依赖需独立 PR，重新解析各平台依赖、获取官方摘要、
复跑源码/最终包检查；不要通过关闭 hash、SQLite 或 notices 检查修绿灯。

## 独立验收与证据

build 产出完整 Dashboard、Node companion、frozen Host、SQLite/search 依赖和通知。
accept 在另一台原生 worker 下载最终归档，核对归档摘要、每个文件/链接与来源，
清除构建库/搜索环境，使用仅含系统工具的 PATH。执行临时 HOME 下的首次安装、
带所有权标记的升级、Host/daemon/search/存储、对话恢复、frozen scope、真实 CI
Keychain/Secret Service 往返和 frozen 删除、keep-config 卸载及用户状态保留。
Linux 钥匙串在 disposable D-Bus/gnome-keyring 服务测试，不使用真实用户密码/API key。
macOS runner 的 Keychain 不等于用户桌面锁定/授权弹窗基线。

包内 build-manifest.json 记录版本、目标、基线、commit/tree、工具链、依赖和文件摘要；
验收记录独立上传，不倒写或篡改已生成包。源码失败注入覆盖未知目录、升级事务、
原生服务删除失败和重试等；不能说所有故障都已在每个真实 OS 上注入。
CI gate 必须等待 runtime matrix、两个 Unix 最终包和 Windows 最终 ZIP 检查；
任一失败/取消/意外跳过不能得到全平台成功。Windows ZIP 还执行 unpacked frozen
Host 和连续性检查；不能将 synthetic ZIP 单测冒充 Windows 原生执行。

真实彩色终端、物理 IME/快捷键、桌面凭据锁定/权限对话仍需实机检查。
macOS 任意 setsid/double-fork 的完整收容未实现，报告 owned-session-only 不完整，
不冒充 OS 沙箱。未参与打包的 Windows 维护者须独立触发、验收并下载两个目标；
作者自己运行不能替代这一条。未完成项目必须保留在 PR 风险中。

## 失败与回滚

先查看 resolve/build/accept 分别在哪一步失败及其 SHA；验收 JSON 仅在全部步骤成功后
上传，缺失不代表成功。不要上传 HOME/.gitgo、原始 trace、真实凭据或数据库作为日志。
如果首次 fork CI 显示审批等待，请维护者批准执行；审批不等于代码批准。

安装只允许替换身份/用户/规范路径匹配的 owned 安装；旧无 marker 包不能自动接管，
请保留原目录并选新路径。失败时按事务提示恢复备份，不手动清理未知目录。
回滚下载旧 commit 的已验收包并按 owned 升级流程安装；凭据索引升级为 schema 2
后旧 reader 未必支持，不能把旧代码覆盖视为无条件安全回滚。卸载清理失败保留
可重试入口；keep-config 保留配置/凭据/状态，运行中 Host 应先退出。
签名/公证与正式 Release 留给维护者受信任流程，fork CI 不接触签名凭据。
