"""统一示例的文件契约、输出提交和可选 Provider 生命周期。

这里的帮助代码不属于 SDK 公共 API，复制示例时可以替换成应用自己的配置层。
"""

import argparse
import importlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_components.core.contracts import Document, ParsedDocument
from agent_components.core.errors import ComponentError


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON，业务契约由下游 Pydantic 对象验证。"""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def document(path: Path, kind: str, document_id: str | None = None) -> Document:
    """读取文本或公共文档契约，保留文本换行和上游来源映射。

    Args:
        path: UTF-8 原文或公共契约 JSON 文件。
        kind: text、document 或 parsed，不能通过文件扩展名猜测。
        document_id: 仅在原文输入时覆盖身份；契约输入保留原身份和版本。

    Returns:
        后续分块、提取和文本偏移共同使用的规范文档。
    """
    if kind == "parsed":
        return ParsedDocument.model_validate(read_json(path)).document
    if kind == "document":
        return Document.model_validate(read_json(path))
    text = path.read_bytes().decode("utf-8-sig")
    digest = sha256(text.encode()).hexdigest()
    return Document(id=document_id or path.name, revision_id=digest, text=text)


def input_args(parser: argparse.ArgumentParser, *, chunks: bool = False) -> None:
    """加入显式的输入形态，避免把 JSON 文件误当成正文。"""
    parser.add_argument("input", type=Path)
    choices = ["text", "document", "parsed"] + (["chunk", "chunks"] if chunks else [])
    parser.add_argument("--input-kind", choices=choices, default="text")
    parser.add_argument("--document-id")


def output_arg(parser: argparse.ArgumentParser) -> None:
    """省略输出路径时向 stdout 输出机器可读 JSON。"""
    parser.add_argument("--output", type=Path)


def protect(output: Path | None, *inputs: Path | None) -> None:
    """在组件产生副作用前拒绝覆盖输入，包括符号链接和硬链接别名。"""
    if output is None:
        return
    for path in inputs:
        if path is not None and (
            output.resolve() == path.resolve()
            or (output.exists() and path.exists() and output.samefile(path))
        ):
            raise ValueError("output must not overwrite an input")


def protect_sqlite(database: Path, *files: Path | None) -> None:
    """拒绝输入输出与 SQLite 主文件、辅助文件及其链接别名冲突。

    Args:
        database: 组件将打开的数据库路径，与工厂相同地先解析符号链接。
        files: 状态输入和 JSON 输出；连接前校验，防止 SQLite 改写这些文件。
    """
    resolved = database.resolve()
    for suffix in ("", "-wal", "-shm", "-journal"):
        protect(Path(str(resolved) + suffix), *files)


def _json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def emit(value: Any, output: Path | None = None) -> None:
    """输出一个 JSON 值；文件通过临时文件原子替换，失败时保留旧结果。"""
    emit_lines(iter([value]), output)


def emit_lines(values: Iterator[Any], output: Path | None = None) -> None:
    """逐条写入 JSONL，不将长文档全部分块加载进列表。

    Args:
        values: 惰性结果迭代器；文件输出完成或失败时显式关闭。
        output: 目标文件；省略时输出 stdout，可能在失败前已有部分输出。

    Notes:
        文件采用同目录原子替换，不能代替数据库事务或知识库版本发布。
    """
    if output is None:
        try:
            for value in values:
                print(_json(value))
        finally:
            close = getattr(values, "close", None)
            if callable(close):
                close()
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as writer:
            name = Path(writer.name)
            for value in values:
                writer.write(_json(value) + "\n")
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(name, output)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)
        close = getattr(values, "close", None)
        if callable(close):
            close()


def run(main: Any) -> None:
    """执行示例并输出不含供应商正文和密钥的错误分类。"""
    try:
        result = main()
    except Exception as exc:
        # 外部 SDK 的 code 也可能是服务端任意文本，只信任本地组件异常分类。
        code = exc.code if isinstance(exc, ComponentError) else "example_error"
        print(
            json.dumps({"error_type": type(exc).__name__, "code": code}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    raise SystemExit(result or 0)


def provider_args(parser: argparse.ArgumentParser) -> None:
    """注入真实 Tokenizer/Embedding，标识须包含模型和配置版本。"""
    parser.add_argument("--tokenizer-factory", help="module:function，无参数工厂")
    parser.add_argument("--tokenizer-id")
    parser.add_argument("--embedding-factory", help="module:function，无参数工厂")
    parser.add_argument("--embedding-id")


@contextmanager
def chunker(args: argparse.Namespace) -> Iterator[Any]:
    """延迟加载 Provider，退出时释放工厂返回的同步连接资源。

    Args:
        args: 包含 Provider 工厂和版本身份的已解析命令参数。

    Yields:
        注入真实 Tokenizer／Embedding 后的 SDK Chunker。

    Raises:
        ValueError: 工厂与身份没有成对提供。
    """
    from agent_components.chunking import Chunker

    with ExitStack() as stack:
        providers = {}
        for kind in ("tokenizer", "embedding"):
            spec = getattr(args, f"{kind}_factory")
            identity = getattr(args, f"{kind}_id")
            if bool(spec) != bool(identity):
                raise ValueError(
                    "provider factory and versioned identity must be supplied together"
                )
            if spec:
                module, name = spec.split(":", 1)
                provider = getattr(importlib.import_module(module), name)()
                close = getattr(provider, "close", None)
                if callable(close):
                    stack.callback(close)
                providers[kind] = provider
                providers[f"{kind}_id"] = identity
        yield Chunker(**providers)
