# 本轮验证结果

环境：Linux x86_64、CPython 3.12.14、uv 0.13.6。验证时间：2026-11-16。

| 检查 | 结果 |
| --- | --- |
| 全量回归 | **149 passed** |
| 分块相关六个测试文件 | **124 passed**，包含解析结构、六种策略、流式、上下文、MMR 和去重 |
| 仅核心依赖环境 | **98 passed、6 个模块 skipped、36 个 integration 用例 deselected** |
| 静态检查 | Ruff check 通过，包含 Google 风格多行接口文档规则 |
| 格式 | src、tests、migrations、scripts 格式检查通过 |
| 类型 | mypy strict：30 个生产源文件无错误 |
| 文档映射 | 45 行行为对照，引用的 54 个独立测试函数均存在 |
| 锁文件 | `uv lock --check` 通过，262 个带平台条件的包记录；本轮未新增第三方依赖 |
| 依赖兼容 | `uv pip check` 通过；238 个已安装包 |
| CPU 配置 | 原依赖基线保持 torch 2.14.1+cpu、torchvision 0.29.2+cpu |
| 大文本资源 | 25,165,824 字符、71,303,169 UTF-8 字节、25,344 个块，完整覆盖 |
| Python 跟踪内存 | tracemalloc 峰值 2,191,924 字节；开启跟踪的处理耗时约 14.65 秒 |
| 本地线程负载 | 八线程，16 次、每次 1,000,000 字符；约 1.57 秒，全部块 ID 摘要与基准一致 |
| 进程峰值内存 | 整个资源检查进程峰值 RSS 38,900 KiB，非模型推理进程 |
| 分发 | wheel／sdist 构建；核心环境安装 wheel 后验证分块、表格、后处理和延迟导入 |

原始测试报告：[pytest.xml](benchmarks/pytest.xml)。资源参数、原始结果与平台信息：[chunking-local.json](benchmarks/chunking-local.json)。资源检查不设任意硬件统一阈值，数字不作为生产 SLA。

## 实际测试条件

- 使用真实 Chonkie RecursiveChunker、句子分隔能力、SemanticChunker，语义提供方为已知确定性向量，不下载模型或调用付费接口。
- 使用原生 tokenizers 离线训练的 BPE 编码器检查真实 Token 上限，并以中文、Emoji、CRLF 和组合字符检查原文保真。
- 真实转换 Markdown、HTML、DOCX；包含原生标题层级、表格、前导零、浮点数字面量，以及整章 Group 回归。
- 使用原生 DoclingDocument 的重复文本及 provenance 检查页码、bbox 和元素映射，精度为元素级，不伪造逐字符页面坐标。
- PDF 分页使用真实五页 PDF 验证页数，并以转换器替身检查原生 page_range 调度和分段身份；**未配置 PDF 权重，因此没有宣称真实 PDF 推理、OCR 精度或 PDF 推理内存验收通过**。
- 仍保留所有原有模型路由、超时、取消、抽取、SQLite 迁移、LanceDB、AgentScope、Skills 和 LangExtract 桥接回归；不把这些基础检查称为整套业务系统交付。

## 复查命令

```sh
uv sync --locked --all-extras
uv run --no-sync pytest -q --junitxml=docs/benchmarks/pytest.xml
uv run --no-sync ruff check src tests migrations scripts
uv run --no-sync ruff format --check src tests migrations scripts
uv run --no-sync mypy src
uv run --no-sync python scripts/export_chunking_review.py
uv run --no-sync python scripts/benchmark_chunking.py --output docs/benchmarks/chunking-local.json
uv pip check
uv build
```

代码检查稿从真实源码生成，包含 SHA-256；生产代码变化后需重新导出。功能和测试一一对应见 [CHUNKING_REVIEW.md](reviews/CHUNKING_REVIEW.md)。CI 已包含脚本检查，但当前未推送远端或运行 GitHub Actions。
