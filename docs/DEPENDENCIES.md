# Python 与依赖基线

确定日期：2026-11-16。版本依据实际 PyPI 元数据、uv 依赖求解及本项目安装/集成检查；不只依据框架文档中的示例版本。

## Python 与包管理

| 项目 | 决定 | 原因 |
| --- | --- | --- |
| Python | **CPython 3.12.14** | 当前环境可用；覆盖 AgentScope 的 Python >=3.11 与其他后端要求，控制首期验证范围 |
| requires-python | `>=3.12,<3.13` | 目前只承诺 3.12 系列；通过额外 CI 后再扩大范围 |
| 包管理 | **uv 0.13.6** | 统一虚拟环境、可选依赖、锁文件和构建 |
| 构建 | hatchling 1.33.0 | 标准 src-layout Python 包 |
| 运行复现 | `uv.lock`＋`uv sync --locked` | 固定直接和传递依赖、来源及哈希 |

不建议在这个项目里直接升级到最新 Python 主次版本；要先验证 PyTorch、Docling、LanceDB 等二进制依赖和全部集成用例。

## 直接依赖与分组

| 分组 | 固定版本 | 用途 |
| --- | --- | --- |
| 核心 | pydantic 2.14.3 | 公共 DTO、Pydantic 输出校验 |
| 核心 | jsonschema 4.26.0 | 动态 JSON Schema 校验 |
| 核心 | tenacity **9.1.4** | 显式有限重试 |
| parsing | docling 2.150.1 | 文件转换 |
| parsing | torch 2.14.1 / torchvision 0.29.2 | 配套 CPU 推理依赖；当前 Linux 实际为 `+cpu` wheel |
| chunking | chonkie 1.7.0 | 递归分块 |
| storage | SQLAlchemy 2.1.8 / aiosqlite 0.22.1 / Alembic 1.20.1 | 异步 SQLite、条件写入与迁移 |
| retrieval | lancedb 0.41.0 | 进程内检索索引 |
| agent | agentscope 2.0.11 | 原生模型、上下文、Skills、权限 |
| agent-storage | agentscope[storage-sql,service] 2.0.11 / aiosqlite 0.22.1 | 原生 SQL Storage 的兼容导入依赖 |
| extraction | langextract 1.7.2 | 原生 Provider 扩展接口与后续长文档证据提取 |
| evaluation | deepeval 4.3.4 / ir-measures 0.4.3 | 后续语义指标与已接入排序指标 |
| dev | pytest 9.2.0 / pytest-asyncio 1.5.0 | 单元和集成检查 |
| dev | ruff 0.17.5 / mypy 2.4.1 / build 1.6.1 | 格式、检查、类型和打包 |

### 已解决的兼容问题

1. **Tenacity 不能直接用最新 9.2.1。** LangExtract 的 google-genai 依赖要求 `<9.2.0`，因此固定为已成功求解的 9.1.4。
2. **Docling CPU 安装。** Linux x86_64 / Windows AMD64 的 torch 与 torchvision 使用显式 PyTorch CPU 索引，不从该索引解析其他包。当前 Linux 实际安装 `torch==2.14.1+cpu`，`torch.version.cuda is None`。
3. **AgentScope SQL 导入。** 2.0.11 的 `agentscope.app.storage` 会先导入 app 入口，仅安装 storage-sql 时实测缺少 apscheduler。因此原生 SQL Store 单独使用 agent-storage 分组，带上所需 service Python 依赖；普通 agent 分组不启用它，也不会因此启动 HTTP、Redis 或其他服务。
4. **不同 AgentScope 返回类型。** 原生 StructuredResponse 是数据类，结果字段为 `content`；适配器经过真实原生方法验证，不假设它是 Pydantic 对象或拥有 `data` 字段。

uv.lock 是跨平台条件锁，可能包含非当前平台分支的包记录；实际安装由平台 marker 决定。已实际验证的环境是 Linux x86_64 / Python 3.12.14，其他平台的 wheel 和行为仍需 CI 验证。

核心和默认依赖不包含 LangChain，不安装 Chonkie 的 all/semantic 等无关扩展。评测、解析和 Agent 都是可选组；安装所有组只是开发验证方式，不是使用单个组件的要求。

## 安装命令

```sh
uv sync --locked
uv run --no-sync agent-components doctor
```

按需选择，例如解析与分块：

```sh
uv sync --locked --extra parsing --extra chunking
```

Agent 原生模型与普通抽取：

```sh
uv sync --locked --extra agent
```

开发/完整后端验证：

```sh
uv sync --locked --all-extras
uv run --no-sync pytest -q
uv pip check
```

生产安装按需附加 `--no-dev`。uv sync 会按声明同步当前环境，切换组合时不要误删需要的 extras；组合命令应完整列出要保留的组。

离线部署需要提前准备对应平台的依赖缓存/wheel 和 Docling 模型产物，锁文件本身不包含模型权重。不会在项目导入时下载 Docling 权重；PDF 默认要求显式 artifacts_path。
