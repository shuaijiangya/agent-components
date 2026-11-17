# 确定性评测组件

源码：[evaluate.py](../../examples/evaluate.py)。当前入口提供结构校验和标准检索指标；安装 DeepEval 不代表已封装裁判模型或生产回归平台。

## Schema 校验

```sh
uv run --no-sync python -m examples.evaluate schema examples/data/contract.expected.json --schema examples/data/contract.schema.json --output data/schema-evaluation.json
```

返回 `passed/errors`，错误包含 JSON Pointer。结构不通过时退出码 1，文件仍保存评测结果。

`evaluate_schema` 只执行 JSON Schema；传 Pydantic 类会先转成 JSON Schema，不执行该类自定义 validator。需要后者时使用提取组件或业务自己的严格模型校验。

此评测不能证明“32000”确实来自合同。抽取事实准确率、缺失率和证据准确率需要真实标注集；`contract.expected.json` 只是一份虚构合同的预期字段，未作为线上准确率结论。

## 检索指标

```sh
uv run --no-sync python -m examples.evaluate retrieval examples/data/ranking.json --labels examples/data/labels.json --k 1 --output data/retrieval-evaluation.json
```

输入分别为 `RankedResult(query_id,item_id,score)` 和 `RelevanceLabel(query_id,item_id,relevance)` 数组。后者应来自人工或可信外部标注；同一 query/item 结果不能重复。

使用真实 `ir_measures` 计算聚合 `R@k/nDCG@k/RR@k`，示例数据只检查 API 与指标计算。生产应固定评测语料版本、查询集、相关性标签和检索配置，分别比较分块策略、召回与候选选择，不用单个查询的分值代表系统质量。
