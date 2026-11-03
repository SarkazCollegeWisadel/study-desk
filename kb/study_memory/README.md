# 每日学习记忆

本模块以真实事件为依据，把明确记录的学习活动整理成短摘要与完整出处 JSON。只用 Python 标准库，不调用模型、不读取聊天记录。`sync` 只读取部署方指定的既有学习日志 API；其他命令不访问网络。模块不创建定时器，调度由部署方自行管理。

## 何时允许记录

只在用户明确说自己读过、学过、练过、作答或复习，或者已有可信的用户实际学习操作回执时记录。`actor` 必须为 `user`，`evidence.kind` 必须为 `explicit_user_statement` 或 `direct_user_action`，且有可追溯 `evidence.ref`。

Agent 搜索测试、自动生成学习稿、课程大纲、播客音频、系统准备资料，都不是用户学过。页面打开或音频生成也不能被解释为已学会。不能为了通过字段验证把 Agent 事件改写成 `user`；本模块校验数据结构和去重，事实真实性由记录者核对证据。`browse` 仅表示明确查阅，`answer` 只表示有作答事件，不表示正确；摘要不推断掌握程度。

## 既有日志的独立层

现用只读接口为 `GET http://127.0.0.1:8765/api/unit/log?n=1000`。返回 `records: [{date, head, body}]`，源实现只保留日期和正文，没有执行者、稳定事件 ID 或具体时间。保存接口既可以被 UI 按钮调用，也可以被 Agent 调用，不能仅由“已进入”和分钟数认定真实学习。

`sync` 将这类旧记录作为 **legacy_note、actor=unknown、date_precision=day** 保存到 `.local/study_memory/legacy.sqlite3` 与 `legacy_notes/`；不写确认事件表，不补具体时刻，不计入当日学习量。明确主题的记忆表述为“既有学习记录记载……是否实际学习未核实”。无明确主题或无有效日期只保留待核索引，不让模型猜。

首次读取标为 `historical_import`，后来新内容标为 `newly_observed_note`。正文内容 hash 幂等，同一条重复同步不新增、不重写索引；内容变化保留新的内容版本。源没有 ID，因此不能宣称已判定某条是编辑还是新增，回执使用 `new_or_changed_notes`。不产生每日旧日志摘要，不自动删除历史版本。

显式 `actor: agent`、`action: audio_generated`、`[agent-test]` 等来源/生成标记排除；正文偶尔提到医学 test 或 AI 不因此删除。未知来源按未核笔记处理，无需要求用户逐条确认历史记录。`legacy_notes/index.json` 供 Agent 查索引，原正文只留本机逐条 JSON；它不是已确认学习摘要。

接口仅允许字面回环地址、固定 GET 路由，不用代理、不跟随重定向、不调用模型或写接口。`n` 为 1–10000，响应上限 8 MiB；返回数量达到窗口上限时明确报告可能截断，不能声称全量迁移。源离线保持已有本地笔记，仍可继续蒸馏已确认事件。

## 存储与版本

- 默认数据目录：`kb/.local/study_memory/`，不依赖当前 shell 的工作目录。
- 权威账本：`events.sqlite3`。事件 ID 不可覆盖；同 ID 同事实为重复，事实不同报冲突。
- 当前摘要：`summaries/YYYY-MM-DD.md` 与 `.json`；完整事件、学习证据、原始来源元数据保存在 JSON 和 SQLite。
- 日界：`Asia/Singapore`（现代日期 UTC+08:00）；按 `occurred_at` 而不是入库时间归日。输入必须带时区，仅支持 1982 年起的现代记录，无需额外 tzdata。
- 同一天内容未变不增版本、不重写文件；迟到事件更新该日期的当前版本，旧版本保留于 `summary_versions`。
- 文件导出中断时，数据库保留已提交事实；再次 `distill` 修复文件，不重复增版。`read` 读取权威数据库。
- `.local/` 已由项目 Git 规则排除。`--data-dir` 仅用于明确的本地私有目录或合成测试临时目录，不能指向公开跟踪目录。
- 本地摘要可供后续 Agent 读取；**外部记忆系统导入尚未实现**。输出中的 `external_memory_imported` 永远为 `false`，不能把文件生成说成已导入记忆。

## 命令

从仓库的 `kb` 目录执行，使用快速开始创建的虚拟环境：

```powershell
$StudyPython = (Get-Item -LiteralPath "../.venv/Scripts/python.exe").FullName
& $StudyPython -B -m study_memory status
& $StudyPython -B -m study_memory sync
& $StudyPython -B -m study_memory record --file '.local/user-study-event.json'
& $StudyPython -B -m study_memory distill
& $StudyPython -B -m study_memory read --date '2026-11-03'
& $StudyPython -B -m study_memory read --date '2026-11-03' --format json
```

也支持 UTF-8 JSON 标准输入：`record --file -`。事件文件是输入载体，默认由调用方放入 `.local/`，不要放在模块目录。命令 `record` 的输出只含 ID、日期和是否插入，不回显学习正文。`status` 只返回数量及待蒸馏日期。

每天一次的官方调度先运行 `sync`，再运行 `distill`，分别保存真实回执；即使日志源离线，也继续 `distill` 处理本地已确认事件。后者检查已有事件日期，包含迟到事件，并修复缺损导出；没有确认事件不创建确认账本或假摘要，`sync` 的历史索引不算学习量。按单日手工恢复可用 `distill --date YYYY-MM-DD`。为检验 CLI 可使用 `--data-dir '<临时目录>'`，该全局参数必须放在子命令之前。

退出码：`0` 成功或没有事件；`2` 无效输入、重复 ID 冲突或存储失败；`3` 请求的日期尚无已蒸馏摘要。出错日志不回显输入正文。不要把 `status` 空结果报成学习遗漏，只说明尚无明确记录。

以下仅为事件结构示例，**不能直接录入真实账本**：

```json
{
  "event_id": "example-learning-001",
  "occurred_at": "2026-11-03T10:00:00+08:00",
  "actor": "user",
  "action": "read",
  "topic": "示例：主诉与现病史",
  "evidence": {
    "kind": "explicit_user_statement",
    "ref": "example:user-confirmation:001"
  },
  "source_refs": [
    {
      "source_id": "example-textbook-edition",
      "title": "示例教材及版本",
      "locator": "问诊章，主诉节"
    }
  ],
  "unresolved": "示例：还需核对的明确问题"
}
```

必需字段为 `event_id/occurred_at/actor/action/topic/evidence/source_refs`。`action` 只接受 `browse/read/study/practice/answer/review`。至少一条出处，每条必须有 `source_id/title/locator`，可选 `uri/content_sha256`；URI、文件 hash 由资料来源提供，本模块不联网核验。可选 `note/unresolved` 仅保存已有明确事实或问题。重复或顺序不同的相同引用归一化，等价时区表示也视为同一事实。

摘要按真实事件量生成：单条阅读产生一条主题记录；大量活动仅展示前 8 个主题/出处，完整事实始终保留在 JSON。没有自动补齐“学习成果”“掌握技能”或不存在的问题。

## 交给 UI／长期 Owner 的接入契约

UI 写入者在真实用户查阅、提交练习或明确点击“我已阅读”后产生事件。事件 ID 在用户动作时生成并持久复用，重试不能换 ID；`occurred_at` 保存动作当时带时区的时间，不能用调度运行时间替代。`evidence.ref` 指向该真实操作回执，`source_refs` 指向当时资料，保留实际主题；只打开资料则用 `browse`，不自动提升为 `study` 或掌握。

后端可在已确认动作后调用 `Ledger(...).record(event)`，或将单事件 JSON 写入本地输入文件，再用本 CLI `record --file`。跨进程推荐标准输入/结构化调用，不将正文拼进 shell 命令。UI 埋点与旧日志导入由部署方接入并自行验收；其他学习动作只能凭明确证据录入，不能把检索或生成行为自动计为学习。

## 测试和接续

```powershell
& $StudyPython -B -m unittest study_memory.test_study_memory -v
```

所有测试使用 `TemporaryDirectory` 和明确标记的合成教材/事件，不写默认 `.local`；HTTP 测试使用临时回环服务器。回滚方式：删除 `.local/study_memory/` 并还原宿主入口文件即可，账本与索引无迁移。

后续 Agent 先看 `status`，再按需 `read` 最近实际日期；不要把 `pending_dates` 或未导入状态解释成业务失败。恢复本模块时保留 `.local/study_memory`，不要删除用户事实账本。每天一次的入口需由部署方自行创建调度并保留实际触发回执；真实用户首次事件验收仍待，不能拿合成测试代替。

## Owner增量：显式查阅与每日入口（0.2.0）

新增 `http_api.py` 供原学习台可选加载；只接受每条检索结果的“记为已查阅”明确确认，记录 `browse`，不推断阅读完成、掌握、用时或正确率。请求携带动作时创建并持久保存的 event_id 与带时区 occurred_at，重试复用同一ID；服务端重新查验当时来源hash/片段hash，来源变更则要求重新检索。Agent不得构造确认请求写入真实账本。`#dry`前端拦截写入，后端显式dry请求只校验、不落盘。

`prepare_study_desk_patch.py` 只向指定私有目录生成原API/HTML的精确补丁和改前SHA，不直接部署。原启动路线不变；接线包只向指定私有目录生成补丁，部署回执由部署方保存在本机，不随公开代码发布。

每天一次的固定入口为 `python -B -m study_memory.daily`，工作目录为本项目。它先只读sync，再无条件distill本机已确认事件；源离线仅报告同步不可用，不阻断既有事实蒸馏。固定入口不创建调度、不调用模型。`daily_status.json`仅记本机执行回执；创建定时任务与实际触发必须另验。

新增测试：`python -B -m unittest study_memory.test_capture -q`。8项合成测试覆盖显式确认、同ID重试、dry不建账本、源变更拒绝、日期/动作边界、ID冲突、源离线仍蒸馏及无事件不造摘要；均使用临时账本。
