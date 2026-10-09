# Study Desk · 学习台

本地学习工作台，围绕「选题 → 推理链 → 证伪 → 大纲落点 → 桶卡 → 下次问题」组织 20 分钟学习单元。Python 后端读取本机资料，网页提供轨道导航、时钟入场、分句证词与判断印章、名解回译、缺口扫描、桶卡抽背、模型配置和 Anki 卡面预览。

## 快速开始

需要 Python 3.10+。默认嵌入接口为本机 Ollama；请自行安装并准备 `bge-m3`。默认生成模型是 `qwen3:14b`，也可接入已有模型或 OpenAI 兼容接口。

仓库根目录执行（Windows PowerShell）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_local.py --root "D:\StudyData"
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

普通文档可以检索；今日单元还依赖文件命名协议，见 [开发说明](docs/DEVELOPMENT.md)。空资料目录不能启动完整服务，需先建索引。

```powershell
cd kb
..\.venv\Scripts\python.exe build_kb.py index
..\.venv\Scripts\python.exe build_kb.py serve --port 8765 --open
```

地址：`http://127.0.0.1:8765/`。环境准备后也可使用 `kb/启动知识库.bat`。Linux/macOS 可创建 venv 并使用 `.venv/bin/python` 运行相同脚本，本次未做这些平台的运行验收。

## 配置与隐私

- `setup_local.py` 从示例创建 `kb/config.json`、`kb/providers.json`，保留已有文件；资料路径写为绝对路径。
- 云端模型在本机 `providers.json` 填入接口、模型与密钥并设置角色绑定；默认所有角色使用本机 Ollama。
- 云端生成会发送检索片段；启用云端嵌入后，索引文本也会发送给相应服务。根据资料权限自行选择。
- 真实配置、资料、学习日志、索引、数据库和个人 Anki 卡包不随源码发布，已设为忽略。
- Anki 工作台只含一张原创演示卡，保留正面、背面与 CSS 下载。个人 99 卡包已从公开 HTML 移除，包下载按钮不可用。
- MIT 覆盖仓库代码与原创示例，不授予外部教材、个人数据或第三方服务内容的许可。

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

本次整理验证了 Python 与内嵌 JavaScript 语法、示例配置解析、本地配置初始化与保留，以及受 Git 跟踪文件和全部提交历史的敏感信息扫描。未执行真实模型请求、索引重建、学习评级或 Anki 同步，历史笔记中的验收不等于本次重新验证。

服务面向本机使用，未提供远程多用户鉴权。HTML 在启动时读入内存，更新后需重启服务。部分评级会写 CSV，试运行前请阅读开发说明。

## 许可证

[MIT](LICENSE) · Copyright © 2026 SarkazCollegeWisadel
