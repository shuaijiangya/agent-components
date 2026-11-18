# 文档解析与离线 PDF

参阅 [解析组件说明](../../src/agent_components/parsing/README.md)、[CLI 源码](../../examples/documents.py)和 [DoclingParser 实现](../../src/agent_components/parsing/docling.py)。

## 标准组件

```sh
uv run --no-sync python -m examples.documents parse examples/data/contract.txt --document-id contract-2026-001 --output data/parsed-txt.json
uv run --no-sync python -m examples.documents parse examples/data/contract.md --document-id contract-2026-001 --output data/parsed-md.json
```

TXT 直接读取文本，保留 CRLF，不导入 Docling。Markdown、HTML、Office 使用 Docling 格式后端。返回 `ParsedDocument`，包含规范 `document.text`、Markdown、原生 JSON，以及可用的 `elements/source_map`。

`Document.text` 是后续字符偏移的唯一基准。`source_map` 中的页码、元素和矩形来自原生解析；规范文本偏移不能线性换算为 PDF 字符位置。没有来源映射时，以 `source_mapping/warnings` 的实际结果为准。

## 非扫描 PDF：两条可落地路径

| 目标 | 标准 PDF 管线 | 原生无模型 PDF 管线 |
| --- | --- | --- |
| 有可选取文本的 PDF 抽出文字 | 支持 | 支持，受 PDF 文本层质量影响 |
| 版面识别、阅读顺序、表格结构恢复 | 使用模型辅助 | 不提供这些模型能力 |
| 标题结构、复杂多栏 | 取决于解析与模型结果，需验收 | 不能依赖其恢复 |
| 扫描页文字 | 可显式开启 OCR | 不支持 OCR |
| 权重文件 | 关闭 OCR 后仍需版面／表格模型 | 不需要模型权重 |
| Torch Python 依赖 | 仍安装并导入 | 仍安装并导入 |

**可选取文本不等于可以无损重建结构。** 普通文字检索可先验收原生管线；需要按 PDF 标题建立父子块、抽表格或还原多栏阅读顺序时，应验收标准管线。

### 标准 PDF：CPU、关闭 OCR、禁止自动下载

在联网制备机用同版本工具准备模型目录，再与 Python 依赖一起搬运到离线机器。离线侧显式传目录：

```sh
uv run --no-sync python -m examples.documents parse /srv/input/contract.pdf --artifacts-path /opt/docling-models --document-id contract-2026-001 --output data/parsed-pdf.json
```

当前 `DoclingParser` 使用 CPU，默认 `do_ocr=False`、`allow_model_downloads=False`；标准 PDF 默认仍启用表格结构恢复，需完整匹配的模型目录。目录存在只能通过路径检查，不能证明权重齐全。仅在确需 OCR、已备齐 OCR 依赖和资源时添加 `--ocr`。

大 PDF 可以分批，输出 **JSONL**：

```sh
uv run --no-sync python -m examples.documents parse /srv/input/contract.pdf --artifacts-path /opt/docling-models --pages-per-batch 10 --output data/pdf-batches.jsonl
```

每批是独立 `ParsedDocument`，文档 ID 带页范围，文本坐标从当前批次的规范文本起算；`metadata.source_document_id/page_start/page_end` 保留所属文件与页范围。不能把每批 `start/end` 当作整篇规范文本的全局字符坐标。

### 原生 PDF：无需模型文件

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run --no-sync python -m examples.documents native-pdf /srv/input/contract.pdf --document-id contract-2026-001 --output data/parsed-native.json
```

关键原生调用为：

```python
from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter, NativePdfFormatOption
from agent_components.parsing import DoclingParser

converter = DocumentConverter(
    allowed_formats=[InputFormat.PDF],
    format_options={InputFormat.PDF: NativePdfFormatOption()},
)
native = converter.convert(path, raises_on_error=True).document
parsed = DoclingParser().from_native(
    native, document_id=document_id, revision_id=revision_id,
)
```

这是对 Docling **原生无模型管线的集成示例**。SDK 当前没有 `DoclingParser(model_free=True)` 开关。该管线仍可能输出页元素、图像或矩形信息；它不会因此具有标题、表格或 OCR 识别能力。应检查空文本、乱码、阅读顺序及保密内容是否遗漏，再决定是否发布到知识库。

## 部署与性能边界

当前依赖锁定 Python 3.12；Linux x86_64 的 Torch CPU wheel 使用 `manylinux_2_28` 基线。离线宿主还需兼容的 CPU 架构、glibc、libstdc++、动态加载器及相关系统库。只检查两个 `.so` 名称，或直接复制另一个机器的 `.venv`，不足以保证可运行；应在目标系统镜像验收依赖导入和真实文件。

模型文件独立于 PyTorch 安装。无 GPU 可使用 CPU，但标准 PDF 性能受页数、分辨率、表格与 CPU 影响。本轮没有下载权重、运行标准 PDF 模型推理或做准确率／吞吐基准；不能据此承诺线上页速。当前 SDK 每次 `parse` 创建 converter，尚无跨文件共享已加载模型的常驻解析 Worker；大批量生产部署需单独评估并发、内存和复用策略。

参考：[Docling 文档](https://docling-project.github.io/docling/)、[Docling 2.150.1](https://github.com/docling-project/docling/tree/v2.150.1)。升级时需重新验证 `NativePdfFormatOption` 和来源映射行为。
