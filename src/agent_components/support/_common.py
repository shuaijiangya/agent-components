"""复用抽取校验、输入范围和单次调用的计量逻辑。"""

import asyncio
import json
import math
import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import JsonValue, ValidationError

from agent_components.core.contracts import (
    Chunk,
    Document,
    ExtractionCoverage,
    ExtractionMeta,
    ExtractionOptions,
    Issue,
    ModelOutput,
    ModelRequest,
    Usage,
)
from agent_components.core.ports import StructuredModel
from agent_components.core.schema import Schema, pointer, validate_data
from agent_components.models.call_logging import model_log_context

type Input = Document | Chunk
type Content = Input | Sequence[Chunk]
type CoverageStatus = Literal["complete", "partial", "failed"]


def inputs_for(content: Content) -> tuple[Input, ...]:
    """展开输入并拒绝会导致来源关系不明确的重复 ID。"""
    inputs = (content,) if isinstance(content, Document | Chunk) else tuple(content)
    if not inputs:
        raise ValueError("At least one Document or Chunk is required")
    if len({item.id for item in inputs}) != len(inputs):
        raise ValueError("Input IDs must be unique")
    return inputs


def input_log_context(
    inputs: Sequence[Input], request_id: str | None
) -> AbstractContextManager[None]:
    """为本次模型调用绑定共同的文档身份。版本只在同一文档内有意义。"""
    documents = {item.id if isinstance(item, Document) else item.document_id for item in inputs}
    revisions = {item.revision_id for item in inputs}
    document_id = next(iter(documents)) if len(documents) == 1 else None
    revision_id = next(iter(revisions)) if document_id is not None and len(revisions) == 1 else None
    return model_log_context(
        request_id=request_id,
        document_id=document_id,
        revision_id=revision_id,
        document_version=revision_id,
    )


@dataclass
class InputProgress:
    """记录一个输入的分段计数，可在外层超时后保留已知的进度。"""

    item: Input
    segments: int = 0
    completed: int = 0
    finished: bool = False

    def snapshot(self) -> ExtractionCoverage:
        """以文档级偏移和实际处理状态生成范围记录。"""
        item = self.item
        status: CoverageStatus = "failed"
        if self.completed:
            status = "complete" if self.finished and self.segments == self.completed else "partial"
        return ExtractionCoverage(
            input_id=item.id,
            document_id=item.id if isinstance(item, Document) else item.document_id,
            revision_id=item.revision_id,
            start=0 if isinstance(item, Document) else item.start,
            end=len(item.text) if isinstance(item, Document) else item.end,
            status=status,
            segments=self.segments,
            completed_segments=self.completed,
        )


class ExtractionRun:
    """跟踪当次抽取的逻辑调用、用量和桥接线程发出的异步请求。"""

    def __init__(
        self, model: StructuredModel, options: ExtractionOptions, inputs: tuple[Input, ...]
    ) -> None:
        """绑定一次抽取的模型端口、运行参数和输入。"""
        self.model = model
        self.options = options
        self.progress = tuple(InputProgress(item) for item in inputs)
        self.issues: list[Issue] = []
        self.calls = 0
        self.started = time.monotonic()
        self._usages: list[Usage] = []
        self._models: dict[str, None] = {}
        self._pending: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def call(self, request: ModelRequest) -> ModelOutput:
        """调用共享模型入口并记录实际返回的用量。"""
        self.check_active()
        task = asyncio.current_task()
        if task is not None:
            self._pending.add(task)
        self.calls += 1
        try:
            response = await self.model.generate(request)
        except BaseException:
            # 失败的调用也可能产生费用。供应商未给出用量时保留未知值。
            self._usages.append(Usage())
            raise
        finally:
            if task is not None:
                self._pending.remove(task)
        self._usages.append(response.usage)
        if response.model:
            self._models[response.model] = None
        return response

    def check_active(self) -> None:
        """在抽取关闭后拒绝线程中延迟到达的请求。"""
        if self._closed:
            raise asyncio.CancelledError

    def close(self) -> None:
        """停止已取消或已超时的线程继续发起异步模型请求。"""
        self._closed = True
        for task in self._pending:
            task.cancel()

    def usage(self) -> Usage:
        """仅当该指标在每次逻辑调用中均已知时返回合计。"""
        inputs = [item.input_tokens for item in self._usages if item.input_tokens is not None]
        outputs = [item.output_tokens for item in self._usages if item.output_tokens is not None]
        count = self.calls
        return Usage(
            input_tokens=sum(inputs) if count and len(inputs) == count else None,
            output_tokens=sum(outputs) if count and len(outputs) == count else None,
            estimated=any(item.estimated for item in self._usages),
        )

    def metadata(self) -> ExtractionMeta:
        """汇总输入范围、模型和调用耗时。"""
        return ExtractionMeta(
            strategy=self.options.strategy,
            request_id=self.options.request_id,
            requested_model_alias=self.options.model_alias,
            models=tuple(self._models),
            model_calls=self.calls,
            duration_seconds=time.monotonic() - self.started,
            coverage=tuple(item.snapshot() for item in self.progress),
        )


def validate_target(
    data: JsonValue, schema: Schema, normalized: dict[str, JsonValue]
) -> tuple[Issue, ...]:
    """依次验证标准 Schema 和 Pydantic 的自定义约束。"""
    try:
        serialized = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError):
        return (Issue(code="invalid_model_json", message="业务值必须是有效的 JSON"),)
    errors = validate_data(data, normalized)
    if errors or not isinstance(schema, type):
        return errors
    try:
        schema.model_validate_json(serialized, strict=True)
    except ValidationError as exc:
        return tuple(
            Issue(code="pydantic_validation", message=error["msg"], path=pointer(error["loc"]))
            for error in exc.errors()
        )
    return ()


def object_schema(properties: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """生成所有声明字段均必填的模型协议对象。"""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def decode_json(raw: JsonValue) -> JsonValue:
    """解码模型协议中的业务 JSON，拒绝重复键及非有限数字。"""
    if not isinstance(raw, str):
        raise ValueError("data_json must be a string")

    def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON object keys")
        return result

    def finite_number(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    return cast(
        JsonValue,
        json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_float=finite_number,
            parse_constant=finite_number,
        ),
    )
