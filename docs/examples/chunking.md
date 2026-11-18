# 分块组件接入

参阅 [分块组件说明](../../src/agent_components/chunking/README.md)、[CLI 源码](../../examples/documents.py)、[Provider 工厂](../../examples/providers.py)和 [完整契约](../CHUNKING.md)。

## 普通分块

```sh
uv run --no-sync python -m examples.documents chunk examples/data/contract.txt --policy examples/data/policy-character.json --document-id contract-2026-001 --output data/chunks.json
```

输入可以是原始文本，或 `--input-kind document` 的 `Document` JSON，或 `--input-kind parsed` 的 `ParsedDocument` JSON：

```sh
uv run --no-sync python -m examples.documents chunk data/parsed-md.json --input-kind parsed --policy examples/data/policy-recursive.json --output data/chunks.json
```

返回 `ChunkResult`：`chunks/config_fingerprint/input_length/covered_ranges/warnings`。`Chunk.text == Document.text[start:end]`，ID 包含内容、文档版本、配置及 Provider 身份摘要。不要丢弃 `warnings`，也不要用 `model_copy` 绕过配置验证。

| 策略 | 配置文件 | 计量／实际依赖 |
| --- | --- | --- |
| 固定字符 | `policy-character.json` | Python 字符；核心 |
| 分隔符 | `policy-delimiter.json` | 字面分隔符，可配置归入前／后块；核心 |
| 递归 | `policy-recursive.json` | Chonkie 原生递归规则；`chunking` |
| 句子 | `policy-sentence.json` | Chonkie 句子切分＋字符上限；`chunking` |
| Token | `policy-token.json` | 真实 Token ID；需 Tokenizer Provider |
| 语义 | `policy-semantic.json` | Chonkie 原生语义比较＋字符上限；`chunking`＋Embedding Provider |

切换上述文件即可切换策略。`size` 只有 Token 策略以 Token 计，其余以字符计；语义阈值并不是模型抽取准确率。表格默认按行保持，超大单行默认报错；只有明确接受后才配置 `table_overflow: "allow"`。

`delimiter` 策略的 JSON 配置使用数组，可填一个分隔符 `"delimiters": ["||"]` 或多个分隔符 `"delimiters": ["。", "||"]`。每个字符串都作为一个完整的字面分隔符。

表格后片重复表头存入 `context_prefix`，原文正文与坐标不变。**索引／提示应使用 `chunk.index_text`**，它是计算属性，不会出现在 `model_dump()` JSON 中；从 JSON 恢复成 `Chunk` 后读取，或显式拼接 `context_prefix + text`。

## Tokenizer：只读本地资源

示例工厂使用 `tokenizers.Tokenizer.from_file`，需要本地 `tokenizer.json`，以及 `parsing` 依赖组包含的 `tokenizers`。它关闭自动添加特殊 Token，模型提示预算需另留空间：

```sh
export TOKENIZER_PATH=/opt/tokenizers/target-model/tokenizer.json
uv run --no-sync python -m examples.documents chunk examples/data/contract.txt --policy examples/data/policy-token.json --tokenizer-factory examples.providers:local_tokenizer --tokenizer-id target-model-tokenizer-sha256-v1 --output data/token-chunks.json
```

务必使用目标模型匹配的 Tokenizer，版本身份应包含资源摘要及编码配置。`size` 不代表整个提示词的最终 Token 数。

## 语义分块：接入真实 Embedding

工厂使用同步 `openai` 客户端（`agent` 组提供）。也可注入你自己的同步 Provider：

```sh
export EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
export EMBEDDING_MODEL=your-embedding-model
export EMBEDDING_DIMENSION=1024
# 通过运行环境设置 EMBEDDING_API_KEY，勿写入仓库。
uv run --no-sync python -m examples.documents chunk examples/data/contract.txt --policy examples/data/policy-semantic.json --embedding-factory examples.providers:compatible_embeddings --embedding-id your-embedding-revision-d1024-v1 --output data/semantic-chunks.json
```

Provider 协议为 `dimension: int`、`embed_batch(texts) -> vectors`。示例验证响应序号并恢复输入顺序，SDK 检查批大小、数量、维度和有限值；工厂退出时关闭连接。不隐式补齐错误向量，不默认下载 Embedding，不使用随机向量冒充语义能力。

## 父子块：章节模式与显式范围

章节模式需要解析结果中的真实标题结构：

```sh
uv run --no-sync python -m examples.documents parents data/parsed-md.json --input-kind parsed --policy examples/data/policy-recursive.json --max-level 2 --output data/parent-child.json
```

没有结构标题时，整篇作为一节；**纯文本中的 `#` 不会自动识别为标题**。`--max-level` 控制作为边界的最大标题层级，`--exclude-preamble` 可排除第一标题前的序言。

使用方已有章节范围时，将 `[[0,120],[120,300]]` 写入 JSON，以 `--ranges path/to/ranges.json` 注入。范围采用规范文本左闭右开字符坐标，可以有间隔，不能重叠、越界。组件先验证全部父范围，再生成两层父子关系。

返回中 `parent_id/ordinal_in_parent/section_path` 可用于父子检索；父块和叶块各自有文档级 `ordinal`。通常仅叶块写向量索引，完整父子语料单独保存以便扩展。如何发布索引由使用方决定。

## 长文本流式分块

```sh
uv run --no-sync python -m examples.documents stream examples/data/contract.txt --policy examples/data/policy-character.json --document-id contract-2026-001 --window-chars 65536 --max-input-chars 100000000 --max-chunks 1000000 --output data/chunks.jsonl
```

只接收已规范化的 UTF-8 文本，不在此命令中解析 PDF。先用临时文件快照确定完整内容身份，再按窗口惰性产出，源文件变化或资源超限明确失败。`window_chars` 不是总输入大小；磁盘快照仍会占空间。语义比较受窗口限制，不等同于整篇一次性语义分析。

不同窗口配置可能改变部分策略的边界，应在目标数据上验收。不要把普通分块与流式分块产物直接混作同一版本发布。
