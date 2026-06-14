# H — 修 Template 下拉 crash

## Read 先
```
Read frontend/workspace/workshop_tab.py L148-L153
```

## Edit

文件: `frontend/workspace/workshop_tab.py`

```python
# old
        templates = TemplateManager.list_templates()
        self.state.template_combo.addItems(templates)
        current = self.state.project.commit_format.get("template_name", "default")
        if current in templates:
```

```python
# new
        templates = TemplateManager.load()
        template_names = [t.name for t in templates]
        self.state.template_combo.addItems(template_names)
        current = self.state.project.commit_format.get("template_name", "default")
        if current in template_names:
```

## 验证
```
grep "TemplateManager\.load()" frontend/workspace/workshop_tab.py
```
