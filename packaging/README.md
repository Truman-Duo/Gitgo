# 原生构建与发布要求

维护者可通过[发布总控](release-controller.md)准备、恢复并交付完整目标矩阵的发行包。

Gitgo 的 Windows 产品包包含 Dashboard 与内部 Native Host，必须一起构建、校验和分发。源码合入不等于二进制已经发布。

## 构建环境

需要 Bun、Rust 1.90.0（含 Cargo）和通过 Gitgo SQLite WAL 安全检查的 Python。运行时由 `GITGO_PYTHON` 或用户级 Gitgo 安装位置选择；脚本不自动改用不安全系统 Python。PyInstaller 可以由 `-PyInstallerPackages` 指定构建依赖目录。安装器还需要已安装的 Inno Setup。

搜索资产需要独立 ripgrep >=14 及匹配的完整第三方许可；可用 `-Ripgrep`、`-RipgrepNotices` 指定。终端验证参考资产通过既有构建参数提供，缺失不能用本机 PATH 掩盖。

## 统一验证入口

```text
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1
powershell -ExecutionPolicy Bypass -File packaging/release_windows.ps1 -BuildInstaller
```

分别进行验证、验证后生成 staging、验证后生成 staging 和安装器。流程包括工作树/提交格式、运行时安全、被跟踪内容扫描、原生 Rust 模块构建、后端/前端测试、打包及冻结后端 smoke。既有扫描未覆盖全部隐私引用，发布候选仍需人工核对。

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
file. A Developer ID identity can be supplied to sign the built binaries.
That alone does not authenticate the full frozen-runtime directory or the
privileged administrative entry point, and does not enable production helper
installation. Apple notarization remains a separate credentialed release step.

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

### macOS stage-four managed-install acceptance

The stage-four candidate has two separate installation authorities. The ordinary
user installs and runs Gitgo; an administrator provisions the project-bound
helper from an independently extracted release package. The helper is copied to
its own root-owned immutable runtime and is not upgraded by a Gitgo app update.

Build a clean source commit with `packaging/build_macos.sh --update-evidence`
and the required build-tool options above. Keep archive creation enabled: it
generates `build-manifest.json`, which managed initial installation requires
alongside the frozen-runtime data contract. Do not edit the manifest to mark a
dirty build as clean. Record the source commit/tree and archive SHA-256.

After verifying and extracting that archive, install Gitgo as the ordinary user:

```sh
./install.sh --managed --no-path-update --install-root /absolute/path/app --bin-dir /absolute/path/bin
```

For local stage-four acceptance, run the administrator helper command from the
**extracted package directory**, using that package's matching Host bundle.
From a trusted checkout, run `python3 -B scripts/macos_helper_digest.py --bundle PATH`
as an ordinary user and compare the result with the trusted build record. Pass
the independently confirmed SHA-256 to the installer:

```sh
sudo ./sandbox-helper.sh install --name example-project \
  --project /absolute/path/project --lifetime 1800 \
  --bundle "$PWD/internal/gitgo-host" \
  --expected-bundle-sha256 "$APPROVED_HELPER_SHA256"
```

Retain the verified package for explicit `status`, `renew`, `upgrade`, and
`uninstall` operations. Do not run `app/active/sandbox-helper.sh` with sudo:
the managed app belongs to its ordinary user and its runtime lease is not a
root administration interface. That location is rejected with
`MACOS_HELPER_MANAGED_LOCATION`; ordinary Host admission is not relaxed.
Project authority expires and is bound to a boot session. Service residency
does not renew it; `renew --after-reboot` is an explicit operator action after
old task cleanup. Uninstall removes the service and credential while retaining
immutable helper versions and recovery evidence.

The package script now enters the frozen Host's fixed `macos-helper-admin`
role. It does not execute an adjacent management Python script. The role
requires real and effective root, a frozen macOS runtime, and the existing
runtime admission checks. This reduces executable inputs; it does not establish
the authenticity of the Host or its libraries before that Host starts.
Root control directories, records, locks, and sealed helper contents must have
no extended ACL entries. An ACL is rejected, never silently removed or repaired.
Candidates and retained recovery images are checked before stopping the current
service, and are checked again when launched.

The reproducible VM acceptance entry is `scripts/check_macos_managed_dashboard.py`.
It requires a disposable VirtualMac, fully enabled SIP, an ordinary login, and
explicit `--run --allow-local-adhoc`. Its arguments are `--package`,
`--source-commit`, `--source-tree`, and an optional new private `--artifact-dir`.
Keep `scripts/macos_dashboard_approval_probe.py` beside it or supply `--probe`.
The probe uses a local fixture provider and real Dashboard input; it verifies
pending/denied publication, single approval, duplicate approval, managed runtime
lock release, public-command reopening, and service/credential cleanup. Logs
and disposable project state are retained. It does not send real provider keys
or touch the user's working projects.

This is internal acceptance of a specific package. Historical test harnesses
can still opt in with `--allow-local-adhoc`. That flag cannot replace a pinned
helper with one that lacks an operator-confirmed digest. Managed initial installation reports
`updates_enabled: false`; the helper reports `production_enabled: false`.
Neither result establishes publisher authentication, notarization, a production
update channel, or compatibility with a different helper release.

### macOS fixed administrator entry and authority acceptance

`scripts/check_macos_helper_entry.py` tests the fixed management role and
preflight refusal in a disposable SIP-enabled VirtualMac. Run it as the ordinary
guest user with passwordless guest administration already configured:

```sh
python3 check_macos_helper_entry.py --run --allow-local-adhoc \
  --payload-root /absolute/path/acceptance-payload \
  --artifact-dir /absolute/path/new-report-directory
```

The payload contains `a/` and `new/`, each with its original `package.tar.gz`
and extracted `payload/`. `plan.json` binds both to the versions, clean source
commit/tree, and archive hashes. Keep `check_macos_cross_version.py`,
`check_macos_managed_dashboard.py`, and `macos_cross_version_helper_probe.py`
beside the entry script. The report directory must be new and outside the payload.

The harness refuses nonroot management, replaces an external management script
with an inert canary, and verifies that the real frozen role still installs.
It rejects an unsafe candidate, damaged current recovery image, damaged recovery
target, and an extended ACL on its unique test marker before stopping a healthy
service. Each case checks service identity and persistent state, then uses a real
ordinary frozen client. A normal-boot recovery preflight has a fixed deadline;
only allocation-lock contention permits a retry. During the real upgrade the
harness briefly holds that existing lock and requires evidence that the new
helper waited in the same startup attempt, became ready, and served the client.
The lock holder always closes its descriptor and has its own hard timeout.
Fault injection is restricted to this run's unique
marker and explicit test maintenance boundary; installation receipts are never
rewritten. The harness removes only its exact test ACE, verifies the marker and
receipt, restores the service, audits task cleanup, and uninstalls its service.
The host-side runner separately verifies absence after normal guest shutdown
and another boot. This does not certify arbitrary historical-version compatibility.

Current helper startup waits at most 30 seconds for the allocation lock within
its 120-second complete-recovery budget. Manager readiness has a fixed 180-second
total deadline and a 40-second inactivity deadline; only current, identity-bound
progress refreshes inactivity. Diagnostics never authorize a client. Older
helpers lacking this progress protocol retain the 40-second inactivity limit.
Large recovery histories can still exceed the budget and must fail visibly;
do not delete recovery evidence to pass an acceptance run.

Exclusive authority directories, metadata and locks reject any nonempty Darwin
ACL, including inherited and deny-only entries. Ordinary project source-file
read access is unchanged. No existing ACL is automatically repaired. See the
[stage-four design and acceptance scope](../docs/MACOS_SANDBOX_DESIGN.md#验证范围)
for the tested package and platform boundaries.

An initial installation can fail before the listener and execution ledger exist.
Cleanup then requires a blocked registration, no invocation or active record,
no ledger, and launchctl reporting no job. Only that proven never-started case
uses normal uninstall without inventing an empty ledger. Other incomplete
evidence retains the service records and fails the acceptance.

`macos_helper_release.inspect_release` is a separate read-only foundation, not
an installer or CLI authorization interface. With a fixed root-owned release
policy it authenticates the raw signed manifest, copies bounded archive bytes
into private staging, checks the complete package contract, and binds the helper
inventory to that package. It runs no candidate code, writes no installation
receipt, and deletes its own staging. Its result is diagnostic only: it does
not authorize later installation, define replay/rollback policy, or enable
production. Publisher identity, bootstrap authentication, trusted receipts and
lifecycle integration still require the agreed release design and acceptance.

### macOS App/helper cross-version acceptance

`scripts/check_macos_cross_version.py` extends the isolated VM acceptance to
independently built App/helper versions. A and B are explicitly local test
versions of the same sandbox protocol, not two published releases or evidence
that arbitrary future protocol changes are compatible. A third clean build,
`failure`, deliberately exits with code 73 from Dashboard `--version`; its
source commit records the fault rather than replacing a binary after building.
Synchronize `packaging/product.json`, `cli/dashboard/package.json`, and
`backend/version.py` for each build, commit the isolated source, and retain its
real commit/tree and archive digest. Build all three complete packages with
`--update-evidence --update-release-policy /absolute/path/local-test-policy.json`.

The quit-only updater used by this matrix is the previously completed updater
patch combined with stage four **only in an isolated acceptance source snapshot**.
It is not a formal integration into this stage-four branch or an upstream
release. Record both source inputs and the updater patch digest in the plan.
Install A into a new managed root so its immutable bootstrap actually supports
API 12 and immediate, post-exit updates. Existing API 11 installations still
retain their reboot contract; never rewrite registration or ledger files to
claim the new capability.

Keep the entry script and these dependencies together in one script directory:

- `check_macos_managed_dashboard.py`: package-contract validation, isolated
  environment, and independent runtime-lock checks.
- `macos_dashboard_approval_probe.py`: the real Dashboard approval flow with a
  loopback fixture provider.
- `macos_cross_version_update_probe.py`: signed local preparation plus actual
  Dashboard update/rollback confirmation and reopening through the public command.
- `macos_cross_version_helper_probe.py`: ordinary-user calls through each active
  App's frozen Host, publication-idempotency checks, and read-only root evidence
  inspection.

Prepare a payload directory with this layout. `payload/` is the complete
extracted package, including its product/build manifests, data contract,
installer, Dashboard, Host, and helper-management entry:

```text
cross-version-input/
  plan.json
  terminal_screen.py          # copy of tests/terminal_screen.py
  a/
    payload/
    package.tar.gz
  b/
    payload/
    package.tar.gz
    manifest.json
    manifest.sig
  failure/
    payload/
    package.tar.gz
    manifest.json
    manifest.sig
  legacy-helper/              # optional retained historical Host bundle
    gitgo-host
    _internal/
```

`plan.json` declares `local_test_only: true`,
`sandbox_protocol_unchanged: true`, `formal_integration: false`, and
`production_publisher_trust: false`. Its `composition` records the stage-four
source input, updater commit range, and updater patch SHA-256. Each of `a`, `b`,
and `failure` supplies `commit`, `tree`, `dirty: false`, `version`, and
`archive_sha256`. Use increasing test versions and release sequences for B and
the failure candidate. Their manifests identify the exact archive bytes, source
commit/tree, platform, version, and real data-compatibility contract.

The detached manifest signatures are real Ed25519 signatures under a temporary,
explicitly local test key. Pin that key's public policy while building A; never
replace the installed policy, forge receipts, or mark dirty source as clean.
The update probe injects only HTTP transport with the exact local manifest,
signature, and archive. The normal preparation service and immutable launcher
still verify signatures, package contracts, and startup health. This exercises
cryptographic verification under test trust; it does not verify a production
publisher, public HTTPS/CDN access, Developer ID, or notarization. Do not put the
private signing key in a package or report.

Run the outer entry with ordinary Python, not a managed frozen Host that would
hold the App's runtime lock. The VM must be a disposable VirtualMac with SIP
fully enabled, and its ordinary login must already be authorized to perform the
test's administrator operations through `sudo -n`:

```sh
python3 /absolute/path/scripts/check_macos_cross_version.py \
  --run --allow-local-adhoc \
  --payload-root /absolute/path/cross-version-input \
  --artifact-dir /absolute/path/new-private-report-directory
```

Use a new or empty private artifact directory outside all extracted packages.
After extracting the inputs, keep at least 3 GiB free for this fixture's retained
versions and failure evidence; the outer entry checks this before installation.
Preserve reports and safely retire completed, unloaded test installations before
repeated runs. Do not lower the application's storage-capacity protection to
make an acceptance run pass.
The entry creates separate App, command, profile, and project directories and a
unique helper service. It tests A/A, B/A, B/B, A/B, then A/A again. App updates
and explicit rollback use real Dashboard confirmation, exit, and reopening of
the original command without rebooting macOS; helper changes use the extracted
package's administrator entry. The matrix also verifies an active App lease
blocks replacement, an inert helper candidate restores the prior helper, and
the source-built failure candidate reaches `CHECK_NEW` / `dashboard-version`
with exit code 73 before restoring exactly the prior App tree. Across version
changes, object reads, already-approved publication retries, file identity,
native invocation cleanup, and existing project grants/credentials are checked.

With `legacy-helper/` present, the plan also records `historical_helper` metadata:
its version, source head, whether it was a dirty snapshot, published-release
status, executable SHA-256, and sealed bundle identity. This retained experimental
helper supplies a real protocol difference: an unsupported checkout operation
must be refused before launch without adding a helper invocation or changing
the project. The current helper is then restored. This negative compatibility
check must not be described as successful full operation against that historical
helper or as acceptance of a clean published historical release.

The entry writes JSON results and retains logs and disposable fixture state,
including failure evidence. It attempts service/credential removal even after
an audit failure, using the lifecycle API's own safety checks; unresolved work
can prevent removal and must remain visible as a failed cleanup. Successful
cleanup requires launchd absence (exit 113), removed credentials/socket/plist,
and clean task evidence. Root-owned immutable helper versions and recovery
records remain retained. Automated PTY interaction is real application evidence,
not a substitute for visible-terminal visual QA or formal release acceptance.

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


### macOS helper protocol compatibility acceptance

`scripts/check_macos_broker_compatibility.py` tests a Host with authenticated
capability negotiation against a retained helper without that operation. This
is distinct from the preceding same-protocol update matrix. Its plan has `a`
and `new` entries, each with `version`, `commit`, `tree`, and `archive_sha256`,
and `local_test_only: true`. Keep each complete package at `<payload>/<key>/payload`
and its archive at `<payload>/<key>/package.tar.gz`. Use a fresh report directory
outside the payload tree and at least 2 GiB free space in the disposable VM.
Keep `check_macos_cross_version.py`, `check_macos_managed_dashboard.py`,
`macos_cross_version_update_probe.py`, `macos_cross_version_helper_probe.py`,
`macos_dashboard_approval_probe.py`, and `terminal_screen.py` beside the entry.

Run as the ordinary VM user with preconfigured administrator authority:

```sh
python3.12 scripts/check_macos_broker_compatibility.py \
  --run --allow-local-adhoc \
  --payload-root /absolute/path/compatibility-inputs \
  --artifact-dir /absolute/path/new-private-report-directory
```

The new Host must refuse the old helper with `HELPER_UPGRADE_REQUIRED` before
sending a functional request; it must not fall back to the old wire behavior.
The tested old Host continues to work with the new helper. Update the helper
first when deploying this new Host, and expect refusal if only the helper is
rolled back. These results do not establish arbitrary future compatibility.
The harness also uses the actual managed Dashboard approval flow, verifies
failed helper replacement, and leaves the managed App and its updater ledger
unchanged throughout helper transitions.

Its last case drops a real `started` acknowledgement at the client. The helper
must execute only once and clean its UID. Normal read-only recovery can confirm
cleanup but must retain `NATIVE_OUTCOME_UNCONFIRMED`; later invocations remain
blocked. The original unknown result and recovery evidence intentionally remain
in the disposable project, including after the service is safely uninstalled.
A successful cleanup report does not mean this business outcome was settled.
The harness never deletes that evidence to produce a passing result.

The source snapshot and separately executed probe scripts have independent
hashes. Retain both, along with package hashes, the full report and a read-only
post-shutdown startup check. Use normal guest shutdown and wait for the VM
process to exit. Local ad-hoc acceptance does not enable publisher trust or
production installation.
