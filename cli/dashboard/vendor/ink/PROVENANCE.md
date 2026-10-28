# Vendored Ink 来源核实记录

核实日期：2026-10-27。此文件记录可核对的来源证据，不授予源码许可，也不替代缺失的上游声明。

## 仓库中已确认的事实

- 路径：`cli/dashboard/vendor/ink`；包名 `@anthropic/ink`，版本字段 `1.0.0`，标为 `private`。
- 首次引入提交：`93567475100dc7083d5c83b85439819445efdb5f`（`[GITGO-33]`）。该提交的 vendor tree 为 `15d0368eca12f9101c218135470a660f206417c6`，package.json blob 为 `aec1f4eebff9757407413e5e2a2f5e06675d0789`。
- 本次开发基线 `03a573c396ce65676b07c7ca142dbfdfae65a583` 的 vendor tree 为 `82c289725ea9e6b1819db0af9b2b6759a6fbc33b`，包含 Alpha3 后续修改。
- 首次引入的提交说明没有登记上游 URL、固定版本或许可文件。基线包目录没有单独的 LICENSE/NOTICE，package.json 没有 license 字段。
- 源码自身使用 `@anthropic/ink` 标识，包含 Anthropic 内部讨论链接，也引用公开 Ink/Yoga 项目。这些标识不能证明整棵目录适用哪个许可。

## 已核对的官方参考

- [公开 Ink 的许可](https://github.com/vadimdemedes/ink/blob/master/license) 是 MIT；仅凭同名或部分共同代码，不能将它当作本目录全部内容的许可证明。
- [Anthropic Claude Code 的许可页](https://github.com/anthropics/claude-code/blob/main/LICENSE.md) 指向其商业条款。这个页面也不能单独证明本目录的具体版本、来源及获授权限。

## 仍需原始提供者补齐

提供本目录所用的上游来源和固定 revision、原始版权/许可声明，以及覆盖本次复制和分发方式的授权依据。当前核实状态为 **unverified**；本次没有给该目录补写 MIT 或 Apache 许可，没有将 Gitgo 项目许可当作第三方授权。

本次 Publish/Windows 修改可通过产品提交与上述基线逐文件比较。依赖的独立许可清单另行随桌面交接资料保存。
