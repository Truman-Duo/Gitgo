# 项目搜索

`list_files` 查找路径，`search_text` 查找内容，默认使用 ripgrep。搜索与其他文件工具经过相同范围与权限检查。

## 常用参数

- `pattern`：内容模式或文件 glob；普通文字建议 `literal: true`。
- `path`、`include`、`exclude`：缩小范围。
- `case_sensitive`：大小写选择。
- `output_mode`：内容、匹配文件或逐文件匹配行数。
- `context_lines`：内容上下文，最多前后各五行。
- `offset`、`max_results`：有界分页；后续页沿用相同查询。
- `include_hidden`、`respect_ignore`：隐藏文件和忽略规则。

结果会说明引擎、范围、是否完整、是否有后续页及警告。返回行数不是整个仓库的匹配总数，分页也不是工作区快照。

## 限制与回退

内容搜索跳过二进制和超过 2MiB 的文件，不跟随符号链接；生成目录和显式排除仍生效。时间、输出和结果数量有上限。

ripgrep 缺失时会提示有限 Python 回退；复杂正则或忽略语义不能默默近似。必要引擎不可用时返回明确错误。部分覆盖不能用来证明内容不存在。

Windows 发布构建要求匹配的 ripgrep 与第三方许可；运行路径与测试命令见 [打包说明](../packaging/README.md)。
