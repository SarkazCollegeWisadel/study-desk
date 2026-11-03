# 来源目录与复习纲要

从已有核验清单、原件、转录文件及课程快照建立本机溯源关系。只调用已有知识库的 loopback 只检索 API，不访问 OCS、不调用 LLM、不修改原件。

`build_catalog.py` 接受显式资料根目录。来源 ID 为内容 SHA-256，分别保留原件、平台转换 PDF、Markdown、章节目录与快照；哈希不符时记录异常，不将该片段用作有效引用。多个课程快照匹配时报告歧义，不任取一个。课程目录不冒称学校正式大纲，旧课表不作为本学期进度。

```powershell
python -B source_catalog/test_catalog.py
python -B source_catalog/build_catalog.py --corpus-root "<课程资料根>" --archive-root "<整理交付目录>" --raw-root "<原下载与快照目录>" --output ".local/source-catalog" --subjects "示例科目"
```

生成 `source-catalog.private.json`、`course-outlines.private.json` 和 `复习来源目录.html`。HTML 可直接打开，列出各科缺项、按 --topics 配置的专题与原文件/行号/物理页；全部生成数据留在 Git 排除的 `.local/`，不进入公开候选。重复生成前保留旧 JSON 的 v-2 副本。

采集日期只采用已有档案中明确的日期，未知时保留 unknown；文件修改时间不当作学习时间。核验发生时间与原采集日期分别记录。仅移除溯源 URL 的认证、查询和片段，原快照保留在本机且不复制到输出。

当前验收侧重来源完整性和引用定位，不将自动关联的专题视为考试权重、模型回答质量或用户已掌握。真实学习事件由独立 `study_memory/` 模块负责。
显式选择至少一个 `--subjects`，不传时立即报错，不生成空课程目录。`--topics topics.json` 可选，省略则只整理逐科来源；JSON 格式为 `[{"title":"示例主题","query":"示例科目 主题","subject":"示例科目"}]`，每项 subject 必须在所选科目中。采集日期仅取档案的 `acquired_on` 或显式 `--acquired YYYY-MM-DD`，否则保留 unknown。`--scope-template`、`--version-markers` 可适配已有目录与版本规则。
