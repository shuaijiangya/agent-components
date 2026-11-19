"""导出按功能模块排列的真实代码 Markdown。

文件内容直接从源码读取，包含摘要与原始行号，避免文档手抄实现后发生漂移。
"""

import ast
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/agent_components/core/contracts.py",
    "src/agent_components/core/errors.py",
    "src/agent_components/chunking/__init__.py",
    "src/agent_components/chunking/chunker.py",
    "src/agent_components/chunking/backends.py",
    "src/agent_components/chunking/sections.py",
    "src/agent_components/chunking/rechunk.py",
    "src/agent_components/core/markdown_tables.py",
    "src/agent_components/chunking/tables.py",
    "src/agent_components/chunking/sources.py",
    "src/agent_components/parsing/__init__.py",
    "src/agent_components/parsing/docling.py",
    "src/agent_components/chunking/streaming.py",
    "src/agent_components/retrieval/context.py",
    "src/agent_components/retrieval/selection.py",
)
CONTRACTS = {
    "Contract",
    "SourceRef",
    "SourceSpan",
    "DocumentElement",
    "Document",
    "TableSlice",
    "Chunk",
    "Issue",
    "ChunkResult",
    "ParsedDocument",
    "ParseProvenance",
}


def main() -> None:
    """生成带中文说明、校验摘要和真实源码的逐文件检查稿。

    导出不会修改生产代码；核心契约仅节选分块有关的类，完整文件随源码包提供。
    """
    content = [
        "# 分块相关代码检查稿\n\n",
        "由 `scripts/export_chunking_review.py` 从真实源码生成。中文注释与代码保持原样，"
        "每节标明路径、SHA-256 和原文件行号。核心契约只节选分块相关类，其余模块完整展示；"
        "节选块不是独立可执行文件，运行请使用源码包。\n\n",
        "功能与参数见 [CHUNKING.md](../CHUNKING.md)，行为、代码与测试的对应关系见 "
        "[CHUNKING_REVIEW.md](../reviews/CHUNKING_REVIEW.md)。\n\n",
        "| 文件 | SHA-256 |\n| --- | --- |\n",
    ]
    for name in FILES:
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        content.append(f"| `{name}` | `{digest}` |\n")
    for name in FILES:
        source = (ROOT / name).read_text()
        lines = source.splitlines()
        content.append(f"\n## {name}\n\n[打开完整源码](../../{name})\n\n")
        if name.endswith("core/contracts.py"):
            for node in ast.parse(source).body:
                if isinstance(node, ast.ClassDef) and node.name in CONTRACTS:
                    content.append(f"### {node.name}（原文件第 {node.lineno} 行）\n\n")
                    content.append(
                        "````python\n"
                        + "\n".join(lines[node.lineno - 1 : node.end_lineno])
                        + "\n````\n\n"
                    )
        else:
            content.append("````python\n" + source.rstrip() + "\n````\n")
    destination = ROOT / "docs/code/CHUNKING_CODE_REVIEW.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(content))
    test_names = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        test_names.update(
            node.name
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        )
    referenced = set(
        re.findall(r"\btest_\w+\b(?!\.py)", (ROOT / "docs/reviews/CHUNKING_REVIEW.md").read_text())
    )
    missing = referenced - test_names
    if missing:
        raise ValueError(f"Review references missing tests: {sorted(missing)}")
    print(f"Exported {len(FILES)} source files; all {len(referenced)} referenced tests exist")


if __name__ == "__main__":
    main()
