# Evidence reasoning checks

原创的证据结构校验与标识符候选过滤代码，配合全合成示例及分层验证研究报告。作为既有 study-desk 的独立增量候选；不包含教材、真实学习轨迹、模型配置或推理服务。

## 本地复现

Python 3.10+，仅使用标准库，无下载、网络、模型或账号需求。在本目录运行：

```sh
python -B -m unittest discover -s evidence_reasoning -t . -p "test_*.py"
python -B -m examples.run_demo
```

标识符过滤只判断片段是否涉及至少一个显式标识，不证明覆盖全部条件。`validate_answer` 接受由调用方保管的可信证据清单；不可用模型自报的清单替代实际工具日志。合成示例中的 provided/fetched 是夹具字段，不是线上工具执行证明。

partial/insufficient必须列至少一条非空文本缺项；取证标志只接受严格布尔True，不接受字符串或数字的真值。

结构校验能拒绝不存在的引用ID、未取证引用、没有结论的supported状态和缺少声明的部分覆盖；无法自动判断引用蕴含、医学正确性或推理合理性。参见 [研究报告](research/REPORT.md)、[许可证来源](SOURCE_LICENSES.json) 和 [第三方说明](THIRD_PARTY_NOTICES.md)。

本组件按已审文件范围增量发布。合成测试与基础工具连通均不证明全部推理场景、临床正确性或完整产品已验收。
