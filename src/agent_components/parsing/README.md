# 文档解析组件

`agent_components.parsing.DoclingParser` 将本地文件转换为规范 `Document`，提取标题层级、表格范围和可用的来源信息。业务方可以把 `parsed.document` 直接交给分块组件，也可以单独使用解析结果。

## 业务逻辑

1. 读取文件并计算 SHA-256，作为文档的 `revision_id`。调用方可传入稳定的 `document_id`，例如业务系统里的合同编号。
2. TXT 直接按 UTF-8 读取，保留原始换行，支持 UTF-8 BOM。Markdown、HTML、DOCX、PPTX、XLSX 和 PDF 通过 Docling 解析。
3. 解析器逐个序列化原生元素，生成规范文本，同时记录标题层级、表格区间和 `source_map`。存在页码或矩形坐标时，映射会保留这些原生来源信息。
4. 返回 `ParsedDocument`，包含 `document`、`markdown` 和 `native_json`。后续分块的字符偏移以 `document.text` 为准，来源定位使用 `source_map`。

## 使用说明

| 方法 | 用途 |
| --- | --- |
| `parse(path, document_id=...)` | 解析一个本地文件 |
| `iter_parse(paths)` | 逐文件消费解析结果 |
| `iter_pdf(path, pages_per_batch=10)` | 按页分批解析 PDF，每批有独立文档 ID 和批次内字符坐标 |
| `from_native(native, document_id=..., revision_id=...)` | 把已有的 Docling 原生文档转换成统一结果 |

以下 Python 示例可分别在仓库根目录运行。TXT 使用核心依赖，其他格式需要 `parsing` 依赖组。

```sh
uv sync --locked --extra parsing
```

如果使用已激活的 Conda 环境，从仓库根目录运行 `UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --locked --inexact --extra parsing`。如还需运行分块的全部示例，一起加上 `--extra chunking`。

## 解析 TXT

```python
from hashlib import sha256
from pathlib import Path

from agent_components.parsing import DoclingParser

path = Path("examples/data/contract.txt")
parsed = DoclingParser().parse(path, document_id="contract-text")

assert parsed.document.revision_id == sha256(path.read_bytes()).hexdigest()
assert parsed.source_mapping == "text_only"
print(parsed.document.id, repr(parsed.document.text[:80]))
```

`text_only` 表示可以按 `document.text` 的字符偏移定位内容，页码和元素坐标取决于解析来源。

## 逐文件解析 Markdown HTML PowerPoint 和 Excel

`iter_parse()` 按输入顺序返回结果，可逐份入库。下面的示例复用仓库中的 Markdown，并在临时目录创建 HTML、PPTX 和 XLSX 输入。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook
from pptx import Presentation

from agent_components.parsing import DoclingParser

with TemporaryDirectory() as directory:
    root = Path(directory)
    html = root / "contract.html"
    html.write_text("<h1>采购合同</h1><p>完工后验收。</p>", encoding="utf-8")

    pptx = root / "delivery.pptx"
    slides = Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[0])
    slide.shapes.title.text = "交付与验收"
    slide.placeholders[1].text = "核对交付范围和验收记录"
    slides.save(pptx)

    xlsx = root / "materials.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["材料", "数量"])
    sheet.append(["合同", 1])
    workbook.save(xlsx)
    workbook.close()

    paths = [Path("examples/data/contract.md"), html, pptx, xlsx]
    parser = DoclingParser()
    for path, parsed in zip(paths, parser.iter_parse(paths), strict=True):
        print(path.name, len(parsed.document.text), parsed.source_mapping)
        assert parsed.document.text
```

## Word 示例

样例使用 Word 的标题样式。解析输出中的 `level` 可直接用于父子分块，`start/end` 可用于查看对应的标题或表格原文。

```python
from pathlib import Path

from agent_components.core.contracts import ParsedDocument
from agent_components.parsing import DoclingParser

parsed = DoclingParser().parse(
    Path("tests/fixtures/parent_child_sample.docx"),
    document_id="purchase-contract",
)

document = parsed.document
for element in document.elements:
    if element.kind in {"heading", "table"}:
        print(element.kind, element.level, document.text[element.start:element.end])

for span in document.source_map[:3]:
    for ref in span.refs:
        print(span.start, span.end, ref.precision, ref.element_id, ref.page, ref.bbox)

payload = parsed.model_dump_json()
assert ParsedDocument.model_validate_json(payload) == parsed
```

该样例会识别文档主标题、两章、四节和一张表。用[父子分块示例](../chunking/README.md#word-父子分块示例)可查看标题如何形成父块。[Word 集成测试](../../../tests/test_word_parent_child.py)

## 用本地模型解析整份 PDF

标准 PDF 管线使用 CPU，默认关闭 OCR。运行前将 `PDF_PATH` 设为本地 PDF 路径，将 `DOCLING_MODEL_DIR` 设为已准备好的 Docling 模型目录。扫描件可额外设置 `DOCLING_OCR=1`，并在模型目录准备所需 OCR 资源。[模型准备与格式说明](../../../docs/examples/parsing.md)

```python
from os import environ
from pathlib import Path

from agent_components.parsing import DoclingParser

parser = DoclingParser(
    artifacts_path=Path(environ["DOCLING_MODEL_DIR"]),
    do_ocr=environ.get("DOCLING_OCR") == "1",
)
parsed = parser.parse(Path(environ["PDF_PATH"]), document_id="contract-pdf")

print(parsed.source_mapping)
print(parsed.document.text[:200])
```

## 按页分批解析 PDF

使用与整份解析相同的环境变量。每批有独立的 `document.id`，元数据保留原文件 ID 和页范围。字符偏移在当前批的 `document.text` 内从零开始。

```python
from os import environ
from pathlib import Path

from agent_components.parsing import DoclingParser

parser = DoclingParser(
    artifacts_path=Path(environ["DOCLING_MODEL_DIR"]),
    do_ocr=environ.get("DOCLING_OCR") == "1",
)
for parsed in parser.iter_pdf(
    Path(environ["PDF_PATH"]), document_id="contract-pdf", pages_per_batch=10
):
    metadata = parsed.document.metadata
    print(
        parsed.document.id,
        metadata["source_document_id"],
        metadata["page_start"],
        metadata["page_end"],
        len(parsed.document.text),
    )
```

## 适配已有 Docling 文档

上游已经完成 Docling 解析时，可以直接适配原生对象。下面在内存中构造最小的原生文档，演示如何设置业务 ID 和版本。

```python
from docling_core.types.doc import DocItemLabel, DoclingDocument

from agent_components.parsing import DoclingParser

native = DoclingDocument(name="purchase-contract")
native.add_heading(text="交付验收", level=1)
native.add_text(label=DocItemLabel.PARAGRAPH, text="验收人按交付清单核对数量。")

parsed = DoclingParser().from_native(native, document_id="contract-001", revision_id="v1")
print(parsed.markdown)
print([(item.level, item.title) for item in parsed.document.elements if item.kind == "heading"])
```

## 读取带文本层的 PDF

设置 `PDF_PATH` 后，可用 Docling 的 `NativePdfFormatOption` 提取 PDF 文本层，再转成统一结构。该管线无需模型权重，适用于已有可选取文本的 PDF。OCR 和模型版面分析使用前面的标准管线。

```python
from hashlib import file_digest
from os import environ
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter, NativePdfFormatOption

from agent_components.parsing import DoclingParser

path = Path(environ["PDF_PATH"])
converter = DocumentConverter(
    allowed_formats=[InputFormat.PDF],
    format_options={InputFormat.PDF: NativePdfFormatOption()},
)
native = converter.convert(path, raises_on_error=True).document
with path.open("rb") as file:
    revision_id = file_digest(file, "sha256").hexdigest()

parsed = DoclingParser().from_native(
    native, document_id="contract-pdf", revision_id=revision_id, filename=path.name
)
print(parsed.document.text[:200])
```

[解析实现](docling.py) · [命令行示例](../../../docs/examples/parsing.md)
