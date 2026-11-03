# Study Desk · 学习台

本地学习工作台，围绕「选题 → 推理链 → 证伪 → 大纲落点 → 桶卡 → 下次问题」组织学习单元。Python 后端读取本机资料，网页提供轨道导航、时钟入场、分句证词与判断印章、名解回译、缺口扫描、桶卡抽背、模型配置和 Anki 卡面预览。

## 快速开始

需要 Python 3.10+。快速开始使用 CPU 文本索引，建库和只检索无需模型服务。生成回答可接入已有模型或 OpenAI 兼容接口；另保留需要 Ollama 的向量索引入口。

仓库根目录执行（Windows PowerShell）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_local.py --root "<your-data-dir>"
```

把有权使用的 Markdown、TXT、CSV 或 PDF 放进配置的资料目录。推荐布局：

```text
StudyData/
  科目名称/
    02_其他内容/
      01_原料_OCR.md
      02_索引_骨架.md
      03_桶_卡片.csv
      扫描版Markdown/
        01-章节切片/
        02-分步学习单元/
  学习记录/
```

普通文档可以检索；今日单元还依赖文件命名协议，见 [开发说明](docs/DEVELOPMENT.md)。先用 setup_local.py 选定资料根目录，再建立索引。`subjects` 为空时收录资料根内符合排除规则的文档；可显式填写科目目录名限制收录并启用查询科目过滤。

```powershell
cd kb
..\.venv\Scripts\python.exe fresh_retrieval.py index
..\.venv\Scripts\python.exe build_kb.py serve --port 8765 --open
```

地址：`http://127.0.0.1:8765/`。环境准备后也可使用 `kb/启动知识库.bat`。Linux/macOS 可创建 venv 并使用 `.venv/bin/python` 运行相同脚本，本次未做这些平台的运行验收。

## 配置与隐私

- `setup_local.py` 从示例创建 `kb/config.json`、`kb/providers.json`，保留已有文件；资料路径写为绝对路径。
- 云端模型在本机 `providers.json` 填入接口、模型与密钥并设置角色绑定；默认所有角色使用本机 Ollama。
- 云端生成会发送检索片段；启用云端嵌入后，索引文本也会发送给相应服务。根据资料权限自行选择。
- 真实配置、资料、学习日志、索引、数据库和个人 Anki 卡包不随源码发布，已设为忽略。
- Anki 工作台只含一张原创演示卡，保留正面、背面与 CSS 下载。个人卡包不随源码发布，包下载按钮不可用。
- MIT 覆盖仓库代码与原创示例，不授予外部教材、个人数据或第三方服务内容的许可。

## 检索、来源与学习记录

- `fresh_retrieval.py index/search/stats` 使用独立 FTS5 索引，不调用模型、不启动服务；既有向量索引不受影响。`build_kb.py index` 则是向量入口，可能自动启动 Ollama 并请求嵌入，只有明确需要该路线时才运行。
- `kb/config.json` 可配置 `subjects`、`version_markers`、`archive_marker`；`topic_globs` 设置今日单元的专题脚本匹配，`legacy_log` 是资料根内的旧日志相对路径。缺项默认为空，不附带任何个人课程。
- [来源目录](source_catalog/README.md) 接受显式科目与专题配置，核对原件、转录关系和页码。生成数据留在 `.local/`，不公开。
- [只读 MCP](integrations/README.md) 向现有宿主提供 search/fetch/outline，安装前核对目标会话的工具作用域。
- [学习记忆](kb/study_memory/README.md) 只把明确点击「记为已查阅」记为 browse；dry 不写入，旧日志不推断掌握。每日入口 `python -B -m study_memory.daily` 从 kb 目录运行；本模块不创建定时任务。

## 文件分工

| 文件 | 职责 |
| --- | --- |
| `kb/build_kb.py` | 分块、增量索引、向量与 BM25 混合检索、引用问答、HTTP 服务 |
| `kb/unit_api.py` | 章节树、候选问、单元进度与记录落盘 |
| `kb/llm_api.py` | 模型适配、角色备选链、证伪与生成 |
| `kb/kb_api.py` | 科目创建、复制导入、索引任务 |
| `kb/drill_api.py` | 名解对照与桶卡复习 |
| `kb/scan_api.py` | 缺口出题、判卷与记录 |
| `kb/tts_step.py` | 可选 Step TTS 工具，密钥由 `STEP_API_KEY` 配置，音色由 `STEP_VOICE_ID` 或 `--voice` 指定 |
| `kb/web/index.html` | 网页界面与原创 Anki 示例 |

## 验证与限制

合成测试覆盖检索配置、只读 MCP、来源完整性及学习记忆事件；可从各模块运行 `unittest`。这些测试不代表模型回答准确率、用户真实学习、定时任务已运行或 Anki 同步。模型回答及宿主工具安装需部署方另行验收。

服务面向本机使用，未提供远程多用户鉴权。HTML 在启动时读入内存，更新后需重启服务。部分评级会写 CSV，试运行前请阅读开发说明。

## 许可证

[MIT](LICENSE) · Copyright © 2026 SarkazCollegeWisadel
