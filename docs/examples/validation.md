# 示例验收记录

验收日期：2026-11-17。Python 3.12.14、uv 0.13.6；依赖沿用仓库锁文件，本次未增加 SDK 依赖或修改 `src` 行为。

| 检查 | 结果 |
| --- | --- |
| 全仓库 pytest | **183 passed**，47.79 秒；此前基线 149 项，本次新增 34 项 |
| 仅核心依赖下的示例测试 | **17 passed / 17 skipped**；跳过项需要对应可选后端 |
| Ruff lint／format | 通过，包含 `examples` |
| mypy `src` | 通过，30 个源码文件；示例没有纳入 strict mypy 范围 |
| `uv lock --check --offline` | 通过 |
| 全依赖环境 `uv pip check` | 通过，238 个已安装包兼容 |
| `uv build --offline` | wheel 与 sdist 构建成功，examples 作为源码包内容交付 |
| 文档实跑 | 7 条真实命令通过：MD 解析、递归分块、章节父子块、流式、两类评测、Skill 枚举 |
| 独立审查 | 完成；错误分类与 SQLite 路径保护修复后复核无遗留 blocker／important，定向回归 9 项通过 |

## 验证覆盖

- TXT→ParsedDocument→ChunkResult 的 CRLF、原文片段与坐标一致性，以及禁止覆盖输入。
- 字符、分隔符、Chonkie 递归／句子、真实本地 Tokenizer、Chonkie 语义分块；后者通过实际 OpenAI 兼容 Embedding HTTP 客户端调用本地测试服务，验证小批次和响应重排。
- Docling 真实 Markdown 标题；空模型缓存、离线模式下的原生 PDF 文本解析，确认无标题／表格模型能力。
- 章节父子关系、父块替换、流式 JSONL、MMR、去重、真实 ir_measures 指标。
- 真实 SQLite 创建、读取、CAS 冲突；主数据库及 WAL／SHM／journal、硬链接／符号链接路径冲突在连接前拒绝。
- 真实 LanceDB upsert 重复执行、向量检索及空允许文档集合。
- 真实 AgentScope/OpenAI 客户端连接本地 HTTP：任务路由、显式别名优先、原生结构错误 fallback、日志正文与供应商错误码隔离。
- 真实 LangExtract Provider 的线程桥接；AgentScope 原生权限、Skill、SQL storage 与快照恢复。
- 真实 TCP 连接成功与关闭端口重试失败；可选依赖未安装时七个入口 `--help` 可用。

源测试：[基础与路径行为](../../tests/test_examples.py)、[框架与传输集成](../../tests/test_examples_integrations.py)。测试服务返回的模型和向量数据均为协议 fixture；没有访问付费模型或下载模型权重。

## 验证边界

没有运行标准 PDF 权重推理、扫描件 OCR、真实模型抽取准确率评测、生产 Embedding 质量评测或吞吐压测。空会话快照与恢复不等于长会话压缩质量验收；LangExtract Provider 也不等于完整字段证据后端。

## 复现命令

```sh
uv sync --locked --all-extras
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests migrations scripts examples
uv run --no-sync ruff format --check src tests migrations scripts examples
uv run --no-sync mypy src
uv lock --check
uv pip check
uv build
```

使用源码包前应确认 `examples` 的 Python 文件和全部 `examples/data` 输入资产都存在；wheel 只分发 SDK。GitHub Actions 已纳入示例 lint、format 和测试；上述记录为本次本地验收，不能替代远端 Actions 的实际运行状态。
