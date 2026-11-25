# 发布总控

从待发布候选自己的源码运行 `python -B scripts/release_controller.py`。需要构建依赖中的 Python 包、Git 和已授权的 GitHub CLI；密钥必须位于源码与发行包之外。总控不自动合并 PR，不选择新版本，也不修改已发布版本。

源码 CLI 的阶段推进已复用 `backend/core/publish/release_engine.py`；Gitgo 的签名、feed 和包结构由独立配方适配。产品 `/publish` 尚未接入这一总控，仍沿用原有发布流程；开发文件回执不能代替产品授权或项目状态。

完整发布总控当前在 Windows/Linux 运行，仍可处理四目标的原生 CI 产物。macOS 通用命令的后代清理尚不能确认，因此在启动前明确拒绝；没有把进程组结束当成完整清理。macOS 只支持计划明确声明 `process_scope: single_process` 的独立检查，沿用现有禁止派生子进程的隔离探针。这不等于 macOS 通用沙箱或完整发布总控已实现。

## 准备计划

先等同一规范提交的 `ci.yml` 完成。它提供 Windows x64、Linux x64/ARM64 和 macOS ARM64 的验收结果。Windows 从同一个已验收的受管 ZIP 生成 Setup 和 Portable，三项一并保存在 `windows-final-zip` 产物中，Unix 各目标交付一个归档。保存公开频道各目标的已签名 manifest/signature；下一版签名会验证来源、递增序列及升级路径。

```sh
python -B scripts/release_controller.py init \
  --plan dev-artifacts/release-next/plan.json \
  --repository OWNER/REPOSITORY --ci-run RUN_ID --sequence NEXT_SEQUENCE \
  --private-key /outside-checkout/publisher.pem --key-id PUBLISHER_KEY_ID \
  --baseline /outside-checkout/previous-feed --notes /outside-checkout/release-notes.md \
  --transition-evidence /outside-checkout/native-transition-reports
```

`init` 自动读取当前提交、tree、版本、频道和完整目标矩阵，并通过只读 API 查找该次 CI 的产物 ID。它不会签名、上传或发布。文件路径按计划文件所在目录解析；生成器使用绝对路径，计划及原始证据不得上传。已有计划不被覆盖。

只发布手动下载时，在 `init` 追加 `--download-page-only`，省略 `--transition-evidence`。这一计划仍要求同提交的四目标 CI、完整附件及签名校验。`published` 只发布版本页，`verified` 并行做匿名摘要核对。需要推进更新频道时，使用频道发布计划和真实跨版本回执，保留已签名版本及其可下载地址。

## 执行与恢复

```sh
python -B scripts/release_controller.py plan --plan dev-artifacts/release-next/plan.json
python -B scripts/release_controller.py run --plan dev-artifacts/release-next/plan.json --evidence dev-artifacts/release-next/evidence
python -B scripts/release_controller.py status --plan dev-artifacts/release-next/plan.json --evidence dev-artifacts/release-next/evidence
```

`run` 默认停在 `prepared`，只读取 GitHub 并在本地准备签名资产。显式指定 `--through draft` 才创建固定标签、草稿并上传；`--through published` 发布并推进现有频道 feed；`--through verified` 再匿名下载所有公开文件核对摘要。执行前应审阅计划、发行说明和对应阶段的外部写入范围。

失败后使用原计划、原 evidence 目录和相同命令恢复。成功且未变化的本地检查、下载和签名会复用；CI、标签、远端资产则重新观察。响应丢失时，不直接重放写入。发现版本、源码、签名输入、远端 feed 或资产冲突会停止，保留历史，不自动回退或删除冲突内容。

`status` 仅显示历史记录，不代表刚刚验证了 GitHub 状态。计划发生变化须使用新的 evidence 目录；过期签名不会被自动续签。不要将另一发布计划的记录复制过来冒充通过。

## 扩展与边界

`checks` 中的本地检查使用 argv 数组、明确输入/输出文件、依赖 `needs` 和超时；`--workers` 控制独立检查及产物下载的并行度。检查必须可重复执行，不得包含发布、推送或其他需要远端对账的写入。外部脚本、工具及影响结果的配置应列入输入。不要把密钥、令牌写进参数或日志。

通过 `--gh-command '["path/to/gh"]'` 可指定 CLI；也可使用已有、等待子进程结束且不泄露凭据的 argv 启动器。禁止在计划中保存凭据。

`prepared` 验证真实归档的来源、完整清单、数据契约、已安装信任和签名；公开下载检查验证匿名交付字节。二者均不代替原生安装、升级/回退、正式终端或人工验收。验收摘要明确保留这些边界。`init` 默认要求同提交的 `CI gate` 成功；额外的验收可加入 `ci` 和 `checks`，不能用旧版本成功记录代替。

## 发布前验证实际版本之间的更新

在 Windows x64、Linux x64/ARM64、macOS ARM64 的原生环境并行运行。上一版使用频道清单所指向的已发布包，下一版使用拟签名发布的最终归档。每个目标运行一次以下命令，集中保存生成的 `<目标>.json` 和 `<目标>.xml`，作为 `--transition-evidence` 输入。

```sh
python -B scripts/check_release_transition.py --previous OLD_ARCHIVE \
  --candidate NEW_ARCHIVE --evidence PRIVATE_REPORT_DIR --scratch-parent PRIVATE_TEST_DIR
```

Windows 的测试父目录和路径祖先须通过原有 NTFS 安全检查。测试使用隔离的安装与项目，复用已有原生升级套件，验证启动、新版本失败恢复、上一版本回滚及受保护数据。需要测试用户退出后接管的目标，须包含真实冻结启动器路径。测试传输为本地注入，真实频道另在发布后匿名回读。源代码 CI 里同一源码递增版本号的构建继续验证机制，不能为正式版本对出具回执。

推进频道前总控会核对每个平台的两份归档摘要、版本、源码身份、所需套件和全部通过结果。任一回执缺失、跳过、失败或归属不同归档时，保留现有更新频道，定位后在新隔离环境重测。发布后回读四个频道清单及签名，核实指向实际可下载的包。针对仍承诺直升的更早版本，逐对补测，或发布已验收的桥接版本。

定期监控更新清单到期时间。签名过期时应在到期前依照既有序列和发布授权维护有效清单，不能靠关闭客户端的签名或时效校验恢复通道。

GitHub 无法原子替换 feed 的 manifest/signature：总控先检查全部基线，再按记录替换，签名先于 manifest。短暂不匹配会使客户端安全拒绝；中断可恢复。它没有跨发布者的服务器事务锁，同一频道须由一个发布者负责，遇到并发修改即停止。不得将其宣称为原子发布。

只有已存在的频道 feed 可以推进；首次建立新频道或切换签名信任需单独审阅。总控不更新旧 Alpha3 的历史资产，也不自行决定桥接迁移策略。

调用者可复用 `scripts.release_controller.run(plan, evidence, remote, through, workers)` 以及只读计划/状态入口。当前尚未接入产品 `/publish`。
