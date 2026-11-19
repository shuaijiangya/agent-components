# 文档解析组件

`agent_components.parsing.DoclingParser` 将本地文件转换为规范 `Document`，提取标题层级、表格范围和可用的来源信息。业务方可以把 `parsed.document` 直接交给分块组件，也可以单独使用解析结果。

## 业务逻辑

1. 读取文件并计算 SHA-256，作为文档的 `revision_id`。调用方可传入稳定的 `document_id`，例如业务系统里的合同编号。
2. TXT 直接按 UTF-8 读取，保留原始换行，支持 UTF-8 BOM。Markdown、HTML、DOCX、PPTX、XLSX 和 PDF 通过 Docling 解析。
3. 解析器逐个序列化原生元素，生成规范文本，同时记录标题层级、表格区间和 `source_map`。存在页码或矩形坐标时，映射会保留这些原生来源信息。
4. 返回 `ParsedDocument`，包含 `document`、`markdown`、`native_json` 和 `source_mapping`。分块的字符偏移以 `document.text` 为准，来源定位使用 `source_map`。

## 使用说明

| 方法 | 用途 |
| --- | --- |
| `parse(path, document_id=...)` | [TXT](#解析-txt)、[Markdown、HTML、PPTX、XLSX](#逐文件解析-markdown-html-powerpoint-和-excel)、[DOCX](#word-示例)、[标准 PDF](#用本地模型解析整份-pdf) |
| `iter_parse(paths)` | [按输入顺序逐文件取得结果](#逐文件解析-markdown-html-powerpoint-和-excel) |
| `iter_pdf(path, pages_per_batch=10)` | [标准 PDF 分页批次](#按页分批解析-pdf) |
| `from_native(native, document_id=..., revision_id=..., filename=...)` | [已有原生对象与 JSON 恢复](#适配已有-docling-文档)、[原生无模型 PDF](#读取带文本层的-pdf) |

`ParsedDocument.provenance` 记录解析后端及版本、源文件 SHA-256 和已知配置。标准 PDF 记录 CPU、OCR、页范围和模型来源，可用 `DoclingParser(model_artifacts_id="...")` 关联已核对的权重清单版本。解析期间检测到源文件改写或替换时会报错，避免沿用旧摘要。外部对象可向 `from_native()` 显式传入已知记录。历史 JSON 缺少此字段时仍可读取。[重新分块与追溯](../../../docs/examples/chunking.md#复用解析产物生成新版本)

以下 Python 代码块可分别保存为仓库根目录下的 `example.py`，通过 `uv run --no-sync python example.py` 运行。TXT 使用核心依赖，其他格式需要 `parsing` 依赖组。标准 PDF 另需模型目录，原生 PDF 需要一份带文本层的本地 PDF。

```sh
uv sync --locked --extra parsing
```

如果使用已激活的 Conda 环境，从仓库根目录运行 `UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --locked --inexact --extra parsing`。如还需运行分块的全部示例，一起加上 `--extra chunking`。

## 解析 TXT

```python
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_components.parsing import DoclingParser

parser = DoclingParser()
path = Path("examples/data/contract.txt")
parsed = parser.parse(path, document_id="contract-text")

assert parsed.document.revision_id == sha256(path.read_bytes()).hexdigest()
assert parsed.document.id == "contract-text"
assert parsed.source_mapping == "text_only"
print(parsed.document.id, repr(parsed.document.text[:80]))

# 规范文本剥离 BOM 并保留 CRLF。版本摘要仍以原始字节计算。
with TemporaryDirectory() as directory:
    source = Path(directory) / "payment.txt"
    text = "付款后发货。\r\n到货后验收。\r\n"
    raw = text.encode("utf-8-sig")
    source.write_bytes(raw)
    parsed = parser.parse(source)
    assert parsed.document.text == parsed.markdown == text
    assert parsed.document.id == parsed.document.revision_id == sha256(raw).hexdigest()
    assert parsed.document.elements == parsed.document.source_map == ()
```

`text_only` 表示可以按 `document.text` 的字符偏移定位内容，页码和元素坐标取决于解析来源。

## 逐文件解析 Markdown HTML PowerPoint 和 Excel

`iter_parse()` 按输入顺序返回结果，支持生成器输入，可逐份入库。下面复用仓库中的 Markdown，并在临时目录创建 HTML、HTM、PPTX 和 XLSX 输入。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook
from pptx import Presentation

from agent_components.parsing import DoclingParser

with TemporaryDirectory() as directory:
    root = Path(directory)
    html = root / "contract.html"
    htm = root / "contract.htm"
    for path in (html, htm):
        path.write_text("<h1>采购合同</h1><p>完工后验收。</p>", encoding="utf-8")

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

    files = [
        (Path("examples/data/contract.md"), "合同总金额"),
        (html, "完工后验收"),
        (htm, "完工后验收"),
        (pptx, "交付与验收"),
        (xlsx, "数量"),
    ]
    parser = DoclingParser()
    results = parser.iter_parse(path for path, _ in files)
    for (path, expected), parsed in zip(files, results, strict=True):
        print(path.name, len(parsed.document.text), parsed.source_mapping)
        assert expected in parsed.document.text
        assert parsed.document.metadata["filename"] == path.name
```

`iter_parse()` 使用 `parse()` 默认的文件摘要身份。需要为每份文件传业务 ID，或隔离单份解析失败时，可以循环调用 `parse(path, document_id=...)` 并在循环内处理异常。迭代器中的解析异常会传给调用方，之前已产出的文档仍可独立处理。

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
        print(element.kind, element.level, document.text[element.start : element.end])

for span in document.source_map[:3]:
    for ref in span.refs:
        print(span.start, span.end, ref.precision, ref.element_id, ref.page, ref.bbox)

payload = parsed.model_dump_json()
assert ParsedDocument.model_validate_json(payload) == parsed
```

该样例会识别文档主标题、两章、四节和一张表。标题和表格区间指向规范 Markdown。`SourceSpan.start/end` 指向整段规范文本，对引用里的 `bbox` 应保留原生元素精度和坐标原点。[父子分块示例](../chunking/README.md#word-父子分块示例) · [Word 集成测试](../../../tests/test_word_parent_child.py)

## 用本地模型解析整份 PDF

标准 PDF 管线使用 CPU，默认关闭 OCR，仍加载版面与表格模型。运行前将 `PDF_PATH` 设为本地 PDF 路径，将 `DOCLING_MODEL_DIR` 设为同版本 Docling 的模型目录。扫描件可额外设置 `DOCLING_OCR=1`，并按所用 OCR 后端准备资源。[模型准备与格式说明](../../../docs/examples/parsing.md)

标准无 OCR 管线可在联网制备机下载 `layout`、`tableformer` 资源，再将目录复制到离线机器。离线进程可设置 `HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1`。模型目录必须存在，权重完整性还需在目标环境执行一次转换确认。

```sh
uv run --no-sync docling-tools models download --output-dir "$DOCLING_MODEL_DIR" layout tableformer
```

```python
from os import environ
from pathlib import Path

from agent_components.parsing import DoclingParser

parser = DoclingParser(
    artifacts_path=Path(environ["DOCLING_MODEL_DIR"]),
    allow_model_downloads=False,
    do_ocr=environ.get("DOCLING_OCR") == "1",
)
parsed = parser.parse(Path(environ["PDF_PATH"]), document_id="contract-pdf")

assert parsed.document.id == "contract-pdf"
print(parsed.source_mapping)
print(parsed.document.text[:200])
for span in parsed.document.source_map[:3]:
    print([(ref.page, ref.bbox, ref.coord_origin) for ref in span.refs])
```

在允许联网获取权重的环境，可显式执行 `DoclingParser(allow_model_downloads=True).parse(Path(environ["PDF_PATH"]))`。

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
previous_end = 0
revision_id = None
for parsed in parser.iter_pdf(
    Path(environ["PDF_PATH"]), document_id="contract-pdf", pages_per_batch=10
):
    document = parsed.document
    metadata = document.metadata
    first, last = metadata["page_start"], metadata["page_end"]
    assert isinstance(first, int) and isinstance(last, int)
    assert first == previous_end + 1 and 1 <= last - first + 1 <= 10
    assert document.id == f"contract-pdf#pages={first}-{last}"
    assert metadata["source_document_id"] == "contract-pdf"
    if revision_id is None:
        revision_id = document.revision_id
    assert document.revision_id == revision_id
    previous_end = last
    print(document.id, len(document.text))
```

页范围从 1 起算且包含首尾页。页批的 `revision_id` 是整份文件的摘要。需要跨批合并表格或章节时，由使用方根据业务规则处理。分批限制单次转换页数，仍需在目标机器测量模型常驻和单页内存开销。

## 适配已有 Docling 文档

上游已经完成 Docling 解析时，可以直接适配原生对象或从 Docling 原生 JSON 恢复对象。下面用重复段落验证两处文字分别关联到各自的页码和元素坐标。

```python
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
)

from agent_components.parsing import DoclingParser

native = DoclingDocument(name="purchase-contract")
native.add_heading(text="交付验收", level=1)
paragraph = "验收人按交付清单核对数量。"
for page in (1, 2):
    native.add_text(
        label=DocItemLabel.PARAGRAPH,
        text=paragraph,
        prov=ProvenanceItem(
            page_no=page,
            charspan=(0, len(paragraph)),
            bbox=BoundingBox(l=10, t=20, r=200, b=40, coord_origin=CoordOrigin.TOPLEFT),
        ),
    )

restored = DoclingDocument.model_validate(native.export_to_dict())
parsed = DoclingParser().from_native(
    restored,
    document_id="contract-001",
    revision_id="v1",
    filename="contract.pdf",
)
assert parsed.source_mapping == "mapped"
mapped_paragraphs = [
    (span.start, span.end, ref.page, ref.bbox)
    for span in parsed.document.source_map
    for ref in span.refs
    if ref.page is not None
]
assert [page for _, _, page, _ in mapped_paragraphs] == [1, 2]
assert mapped_paragraphs[0][0] < mapped_paragraphs[1][0]
for start, end, _, bbox in mapped_paragraphs:
    assert parsed.document.text[start:end] == paragraph
    assert bbox == (10, 20, 200, 40)
    print(start, end, bbox)
```

`from_native()` 使用调用方提供的 `revision_id`。解析结果的 `native_json` 可用于恢复原生模型对象。写入业务存储时，也可以直接序列化 `ParsedDocument`，保留规范文本与映射。

## 读取带文本层的 PDF

设置 `PDF_PATH` 后，可用 Docling 的 `NativePdfFormatOption` 提取 PDF 文本层，再转成统一结构。该管线无需模型权重，仍使用 `parsing` 依赖组，适用于已有可选取文本的 PDF。需要 OCR、标题识别、复杂多栏阅读顺序或表格结构时，应使用标准管线并按真实样本验收。

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
if not parsed.document.text.strip():
    raise ValueError("PDF 的文本层为空，请检查文件或使用 OCR 管线")
print(parsed.document.text[:200])
```

`NativePdfFormatOption` 是 Docling 原生接口，可配合 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 在离线环境运行。解析出的页元素和坐标以 `source_mapping` 和 `source_map` 为准。按页批量转换的 SDK 入口 `iter_pdf()` 使用标准管线。

## 处理输入错误

| 情况 | 调用方处理 |
| --- | --- |
| 文件不存在、无法读取 | 捕获 `OSError`，检查路径和读取权限 |
| TXT 编码不兼容 | 捕获 `UnicodeDecodeError`，将源文件转成 UTF-8 |
| 非 TXT 缺少 `parsing` 依赖 | 捕获 `MissingDependencyError`，按锁文件安装该依赖组 |
| 不支持的扩展名 | 检查 `.txt`、`.md`、`.html`、`.htm`、`.docx`、`.pptx`、`.xlsx`、`.pdf` |
| 离线标准 PDF 未配置本地模型目录 | 捕获 `ValueError`，准备并传入 `artifacts_path` |
| PDF 页批参数无效 | 在消费迭代器时会抛 `ValueError`，检查是否为 PDF 且批大小为正整数 |
| Docling 转换失败 | 由 Docling 抛出异常，单独记录失败文件或页批，原始错误类型由后端决定 |

下面直接验证不带本地模型目录的标准 PDF 请求会被提前拒绝。临时 PDF 只用于验证入口检查，页面未进入转换器。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_components.parsing import DoclingParser

with TemporaryDirectory() as directory:
    path = Path(directory) / "contract.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    try:
        DoclingParser().parse(path)
    except ValueError as exc:
        assert "artifacts_path" in str(exc)
    else:
        raise AssertionError("标准管线应要求本地模型目录")
```

## 命令行复现

`parse` 和 `native-pdf` 默认输出 `ParsedDocument` JSON。标准 PDF 加 `--pages-per-batch` 后输出 JSONL，每行对应一个批次。`--output` 让示例命令在转换成功后提交结果文件。

```sh
uv run --no-sync python -m examples.documents parse tests/fixtures/parent_child_sample.docx --document-id contract-001 --output data/word.json
uv run --no-sync python -m examples.documents parse "$PDF_PATH" --artifacts-path "$DOCLING_MODEL_DIR" --output data/standard.json
uv run --no-sync python -m examples.documents parse "$PDF_PATH" --artifacts-path "$DOCLING_MODEL_DIR" --pages-per-batch 10 --output data/page-batches.jsonl
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run --no-sync python -m examples.documents native-pdf "$PDF_PATH" --output data/native.json
```

[解析实现](docling.py) · [命令行示例](../../../docs/examples/parsing.md)
