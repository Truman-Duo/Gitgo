# i18n — Round 3 所有新增 key

## 执行方式

分别打开 `locales/zh.json` 和 `locales/en.json`，在文件**倒数第三行** `}` 之前追加。

每个文件的最后三行目前是：
```json
  "history.filter_governance": "Governance Events"
}
```

给倒数第二行末尾加逗号，然后在 `}` 前插入新 key。

---

## zh.json 追加

在最后的 `}` 前插入：

```json
  "gov.snap_now": "立即快照",
  "gov.snap_list": "快照列表",
  "gov.snap_restore": "恢复最新",
  "gov.snap_ok": "已快照: {files}",
  "gov.snap_empty": "无记忆文件可快照",
  "gov.snap_dialog": "Memory Snapshots",
  "gov.snap_none": "没有快照记录",
  "gov.restore_this": "恢复此版本",
  "gov.restore_confirm_title": "确认恢复",
  "gov.restore_confirm": "将用快照覆盖 workspace 中的 .claude/ .codex/ .codebuddy/？",
  "gov.restore_ok": "已恢复: {files}",
  "gov.restore_fail": "恢复失败: {e}",
  "gov.update_contract": "Update",
  "gov.update_contract_dialog": "更新合约",
  "gov.update_hint": "添加一条 decided feature（已存在则增加确认计数）：",
  "gov.feature_name": "名称:",
  "gov.feature_location": "文件:",
  "gov.feature_sig": "签名:",
  "gov.name_required": "名称不能为空",
  "lesson.dialog_title": "Lesson 管理",
  "lesson.search_placeholder": "搜索...",
  "lesson.search": "搜索",
  "lesson.tab_instance": "Instance",
  "lesson.tab_abstract": "Abstract",
  "lesson.tab_pending": "Pending",
  "lesson.verify": "Verify",
  "lesson.promote": "Promote",
  "lesson.verified": "已验证: {id}",
  "lesson.promoted": "已提升: {id}",
  "lesson.promote_title": "提升为抽象层",
  "lesson.promote_hint": "输入 tech_stack（如 PySide6）：",
  "lesson.not_found": "未找到",
  "action.template_mgr": "管理模板",
  "template.dialog_title": "模板管理",
  "template.list_header": "模板列表:",
  "template.add": "＋ 新建",
  "template.delete": "✕ 删除",
  "template.edit_header": "编辑:",
  "template.name": "名称:",
  "template.desc": "描述:",
  "template.header_fmt": "Header 格式:",
  "template.body_fmt": "Body 格式:",
  "template.save": "保存",
  "template.confirm_delete": "删除模板「{name}」？",
  "template.saved": "模板已保存: {name}",
```

## en.json 追加

```json
  "gov.snap_now": "Snapshot Now",
  "gov.snap_list": "Snapshots",
  "gov.snap_restore": "Restore Latest",
  "gov.snap_ok": "Snapshotted: {files}",
  "gov.snap_empty": "No memory files to snapshot",
  "gov.snap_dialog": "Memory Snapshots",
  "gov.snap_none": "No snapshots found",
  "gov.restore_this": "Restore This",
  "gov.restore_confirm_title": "Confirm Restore",
  "gov.restore_confirm": "Overwrite .claude/ .codex/ .codebuddy/ in workspace with snapshot?",
  "gov.restore_ok": "Restored: {files}",
  "gov.restore_fail": "Restore failed: {e}",
  "gov.update_contract": "Update",
  "gov.update_contract_dialog": "Update Contract",
  "gov.update_hint": "Add a decided feature (existing ones get confirmation count +1):",
  "gov.feature_name": "Name:",
  "gov.feature_location": "File:",
  "gov.feature_sig": "Signature:",
  "gov.name_required": "Name required",
  "lesson.dialog_title": "Lesson Manager",
  "lesson.search_placeholder": "Search...",
  "lesson.search": "Search",
  "lesson.tab_instance": "Instance",
  "lesson.tab_abstract": "Abstract",
  "lesson.tab_pending": "Pending",
  "lesson.verify": "Verify",
  "lesson.promote": "Promote",
  "lesson.verified": "Verified: {id}",
  "lesson.promoted": "Promoted: {id}",
  "lesson.promote_title": "Promote to Abstract",
  "lesson.promote_hint": "Enter tech_stack (e.g. PySide6):",
  "lesson.not_found": "Not found",
  "action.template_mgr": "Manage Templates",
  "template.dialog_title": "Template Manager",
  "template.list_header": "Templates:",
  "template.add": "＋ New",
  "template.delete": "✕ Delete",
  "template.edit_header": "Edit:",
  "template.name": "Name:",
  "template.desc": "Description:",
  "template.header_fmt": "Header Format:",
  "template.body_fmt": "Body Format:",
  "template.save": "Save",
  "template.confirm_delete": "Delete template \"{name}\"?",
  "template.saved": "Template saved: {name}",
```

## 验证
```
grep "gov.snap_now" locales/zh.json
grep "gov.snap_now" locales/en.json
```
