# 版本记录

> 格式: v主版本.次版本 (日期)

---

## v0.2 (2026-05-08)

**CUI 同步** — 终端界面功能等效于 GUI

- CUI 支持多项目管理（项目列表 → 选择 → 操作）
- CUI 支持 box commit 合并 + Sync/Push 分离
- 修复 `--mode config` 适配多项目输出

---

## v0.1 (2026-05-08)

**初始版本**

- GUI 和 CUI 双界面
- 文件 SHA256 对比扫描
- Commit 整合：多 workspace commit 合并为正式 commit
- Sync 到备份仓库 + Push 到 GitHub（分离操作）
- 多项目管理：ProjectConfig + 项目列表首页
- 旧配置自动迁移
