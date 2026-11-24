"""记录可关联的模型调用、后端尝试和实际 HTTP 发送事件。

应用可复用 Python logging，或选择标准输出和本地滚动 JSONL 文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Literal, Self, TypedDict, Unpack
from uuid import uuid4

from pydantic import JsonValue, SecretStr

from agent_components.core.contracts import Usage
from agent_components.core.errors import ComponentError

MODEL_CALL_LOGGER_NAME = "agent_components.model_calls"
_logger = logging.getLogger(MODEL_CALL_LOGGER_NAME)
_sink_lock = RLock()
_sink: logging.Handler | None = None
_include_bodies = False


def _report_write_failure() -> None:
    # logging.handleError 默认可能把整个 LogRecord 写入 stderr，包含已获准写入文件的正文。
    try:
        sys.stderr.write("agent_components: model call log write failed\n")
    except (OSError, ValueError):
        pass


class _JsonlHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _report_write_failure()


class _StdoutHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        print(record.getMessage(), file=sys.stdout, flush=True)


_stdout = _StdoutHandler()


def configure_model_logging(
    *,
    destination: Literal["stdout", "jsonl"] = "stdout",
    path: str | Path | None = None,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    include_bodies: bool = False,
) -> None:
    """为当前进程选择唯一日志落点和正文策略，可重复配置。

    Args:
        destination: stdout 写标准输出，jsonl 使用按大小轮转的文件。
        path: JSONL 文件路径，父目录须已存在。
        max_bytes: 文件轮转的字节阈值，单条事件总是完整写入。
        backup_count: 保留的轮转备份数，另有一个当前文件。
        include_bodies: 允许记录脱敏后的输入、输出正文，默认只记录长度。

    Raises:
        ValueError: 落点、路径或保留参数无效。
        OSError: 文件无法打开。
    """
    global _sink, _include_bodies
    if type(include_bodies) is not bool:
        raise ValueError("include_bodies must be a boolean")
    with _sink_lock:
        handler: logging.Handler
        if destination == "stdout" and path is None:
            handler = _stdout
        elif destination == "jsonl" and path is not None:
            if type(max_bytes) is not int or max_bytes < 1:
                raise ValueError("max_bytes must be a positive integer")
            if type(backup_count) is not int or backup_count < 1:
                raise ValueError("backup_count must be a positive integer")
            handler = _JsonlHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        else:
            raise ValueError("choose stdout without a path or jsonl with a path")
        previous = _sink
        _sink, _include_bodies = handler, include_bodies
        if isinstance(previous, _JsonlHandler):
            previous.close()


class _Correlation(TypedDict, total=False):
    request_id: str | None
    run_id: str | None
    kb_id: str | None
    document_id: str | None
    revision_id: str | None
    document_version: str | None
    job_id: str | None
    extraction_job_id: str | None


_context: ContextVar[_Correlation | None] = ContextVar("model_log_context", default=None)


@contextmanager
def model_log_context(**fields: Unpack[_Correlation]) -> Iterator[None]:
    """在本线程或异步任务中绑定关联标识，退出时恢复父作用域。

    Args:
        **fields: 可提供 request_id、run_id、kb_id、document_id、revision_id、
            document_version、job_id、extraction_job_id。省略继承，显式 None 清除。
    """
    if not fields.keys() <= _Correlation.__annotations__.keys():
        raise ValueError("unknown model log context field")
    context: _Correlation = (_context.get() or {}).copy()
    context.update(fields)
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def current_request_id() -> str | None:
    """读取作用域的请求标识，供下游构造关联请求。"""
    return (_context.get() or {}).get("request_id")


_MASK = "[REDACTED]"
_CREDENTIAL_NAMES = (
    r"api[-_ ]?key|api[-_ ]?token|access[-_ ]?key(?:[-_ ]?id)?|access[-_ ]?token|"
    r"refresh[-_ ]?token|auth[-_ ]?token|id[-_ ]?token|secret(?:[-_ ]?(?:key|access[-_ ]?key))?|"
    r"pass(?:word|wd)|private[-_ ]?key|credential(?:s)?|token|authorization|cookie"
)
_SECRET_FIELD = re.compile(rf"(?:{_CREDENTIAL_NAMES})$", re.I)
_ASSIGNMENT = re.compile(
    rf"""(?ix)((?<![^\W_])(?:{_CREDENTIAL_NAMES})["']?\s*[:=]\s*)"""
    r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)"""
)
_AUTH = re.compile(r"""(?i)\b(?:Bearer|Basic)\s+([^\s,;"'}\]]+)""")
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_EVENT_FIELDS = frozenset(
    {
        "model_alias",
        "from_alias",
        "to_alias",
        "model",
        "provider",
        "reason",
        "matched_rule",
        "policy_id",
        "policy_version",
        "requested_model_alias",
        "input_tokens",
        "estimated",
        "candidates",
        "attempt_number",
        "error_type",
        "error_code",
        "batch_size",
        "streaming",
    }
)


class _Redactor:
    def __init__(self, secrets: Iterable[str | SecretStr]) -> None:
        self.secrets = {
            key.get_secret_value() if isinstance(key, SecretStr) else key for key in secrets if key
        }

    def collect(self, value: JsonValue, *, credential: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                self.collect(item, credential=credential or bool(_SECRET_FIELD.search(key)))
        elif isinstance(value, list):
            for item in value:
                self.collect(item, credential=credential)
        elif isinstance(value, str):
            if credential and value:
                self.secrets.add(value)
            self.secrets.update(m.group(1) for m in _AUTH.finditer(value))
            for match in _ASSIGNMENT.finditer(value):
                self.secrets.add(match.group(2).strip("\"'"))

    def text(self, value: str) -> str:
        # 先处理带语法的凭证，再替换配置密钥及正文凭证的裸值（包括供应商回显）。
        value = _AUTH.sub("Bearer " + _MASK, value)
        value = _ASSIGNMENT.sub(lambda match: match.group(1) + _MASK, value)
        value = _API_TOKEN.sub(_MASK, value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, _MASK)
        return value

    def scrub(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {
                self.text(key): _MASK if _SECRET_FIELD.search(key) else self.scrub(item)
                for key, item in value.items()
            }
        return value


def _outcome(error: BaseException | None, completed: bool) -> str:
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)) or (not error and not completed):
        return "cancelled"
    return "completed" if error is None else "failed"


def _failure(error: BaseException | None) -> dict[str, JsonValue]:
    if error is None:
        return {}
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        code = "cancelled"
    elif isinstance(error, TimeoutError):
        code = "timeout"
    elif isinstance(error, ComponentError):
        code = error.code
    else:
        code = "model_error"
    details: dict[str, JsonValue] = {"error_code": code, "error_type": type(error).__name__}
    status = getattr(error, "status_code", None)
    if type(status) is int and 100 <= status < 600:
        details["http_status"] = status
    return details


class ModelCall:
    """将一次逻辑调用、路由事件和后端尝试关联起来。"""

    def __init__(
        self,
        task: str,
        *,
        request_id: str | None = None,
        alias: str | None = None,
        operation: str,
        body: JsonValue = None,
        secrets: Iterable[str | SecretStr] = (),
    ) -> None:
        """保存关联快照，使流可在其他任务中关闭。

        Args:
            task: 业务任务名。
            request_id: 显式请求标识；缺省时继承作用域或生成标识。
            alias: 调用方指定的模型别名。
            operation: 模型操作类型。
            body: 可选输入，用于长度统计和显式启用的正文记录。
            secrets: 供应商配置的密钥，可从自由文本、字段名和响应回显中脱敏。
        """
        context = _context.get() or {}
        self._context: dict[str, JsonValue] = {
            k: v for k, v in context.items() if v is None or isinstance(v, str)
        }
        self._context.update(
            call_id=uuid4().hex,
            request_id=request_id or current_request_id() or uuid4().hex,
            task=task,
            operation=operation,
            requested_model_alias=alias,
            model_alias=alias,
        )
        self._redactor = _Redactor(secrets)
        self._body = body
        self._allow_bodies = _include_bodies
        self._started = time.monotonic()
        self._attempts = 0
        self._retries = 0
        self._fallbacks = 0
        self._model: str | None = None
        self._usage: Usage | None = None
        self._completed = False

    def _emit(
        self,
        event: str,
        *,
        request_body: JsonValue = None,
        response_body: JsonValue = None,
        **details: JsonValue,
    ) -> None:
        try:
            with _sink_lock:
                if _sink is None and not _logger.isEnabledFor(logging.INFO):
                    return
                row = {
                    **self._context,
                    **details,
                    "event": event,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                for name, length, body in (
                    ("request_body", "input_length", request_body),
                    ("response_body", "output_length", response_body),
                ):
                    if body is not None:
                        row[length] = len(
                            body
                            if isinstance(body, str)
                            else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                        )
                        if self._allow_bodies and _include_bodies:
                            self._redactor.collect(body)
                            row[name] = body
                row_json = json.dumps(
                    self._redactor.scrub(row), ensure_ascii=False, separators=(",", ":")
                )
                if _sink is None:
                    _logger.info(row_json)
                else:
                    record = _logger.makeRecord(
                        MODEL_CALL_LOGGER_NAME, logging.INFO, __file__, 0, row_json, (), None
                    )
                    _sink.handle(record)
        except Exception:
            # 记录失败不传播给执行器，否则可能导致同一次业务请求再次发往供应商。
            _report_write_failure()

    def __enter__(self) -> Self:
        """记录逻辑调用开始。"""
        self._started = time.monotonic()
        self._emit("model.started", request_body=self._body)
        self._body = None
        return self

    def event(self, event: str, **safe_fields: JsonValue) -> None:
        """记录路由、重试或回退事件，只接受白名单内的元数据。"""
        if event == "model.retry":
            self._retries += 1
        elif event == "model.fallback":
            self._fallbacks += 1
        elif event == "model.route":
            alias = safe_fields.get("model_alias")
            if isinstance(alias, str):
                self._context["model_alias"] = alias
        else:
            raise ValueError("unknown model decision event")
        self._emit(event, **{k: v for k, v in safe_fields.items() if k in _EVENT_FIELDS})

    def attempt(self, alias: str, *, model: str | None = None) -> ModelAttempt:
        """创建独立尝试，在进入作用域后分配顺序号并记录开始。"""
        return ModelAttempt(self, alias, model)

    def complete(
        self,
        *,
        model: str | None = None,
        usage: Usage | None = None,
        body: JsonValue = None,
    ) -> None:
        """登记结果，在逻辑调用退出时输出一次终态事件。"""
        self._completed = True
        self._model = model or self._model
        self._usage = usage if usage is not None else self._usage
        self._body = body

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """记录最终状态、耗时和已知的最后一次尝试用量。"""
        status = _outcome(exc, self._completed)
        self._emit(
            f"model.{status}",
            status=status,
            model=self._model,
            usage=self._usage.model_dump() if self._usage is not None else None,
            duration_seconds=time.monotonic() - self._started,
            attempt_count=self._attempts,
            retry_count=self._retries,
            fallback_count=self._fallbacks,
            response_body=self._body if status == "completed" else None,
            **_failure(exc),
        )


_bound_attempt: ContextVar[ModelAttempt | None] = ContextVar("model_http_attempt", default=None)


class ModelAttempt:
    """记录一次后端调用及该尝试内观测到的 HTTP 请求。"""

    def __init__(self, call: ModelCall, alias: str, model: str | None) -> None:
        """保存当前别名和供应商模型标识。"""
        self._call = call
        self._alias = alias
        self._model = model
        self._details: dict[str, JsonValue] = {"attempt_id": uuid4().hex, "model": model}
        self._started = time.monotonic()
        self._sent = False
        self._closed = False
        self._send_lock = Lock()
        self._completed = False

    def __enter__(self) -> Self:
        """记录独立尝试开始，不绑定生成器消费者的上下文。"""
        self._call._context["model_alias"] = self._alias
        self._call._model = self._model
        self._call._usage = None
        self._call._attempts += 1
        self._details["attempt_number"] = self._call._attempts
        self._started = time.monotonic()
        self._call._emit("model.attempt.started", **self._details)
        return self

    @contextmanager
    def bind(self) -> Iterator[None]:
        """仅在供应商操作或单次 anext 期间让 HTTP 钩子找到该尝试。"""
        token = _bound_attempt.set(self)
        try:
            yield
        finally:
            _bound_attempt.reset(token)

    def wire_request(self) -> None:
        """在传输前记录一次发送；拦截尝试内的隐式重试和重定向。"""
        with self._send_lock:
            if self._closed or self._sent:
                raise RuntimeError("a model attempt can send at most one HTTP request")
            self._sent = True
        self._call._emit("model.http.request", **self._details)

    def complete(self, usage: Usage | None = None) -> None:
        """登记本次尝试的成功结果和供应商返回的用量。"""
        self._completed = True
        self.observe_usage(usage)

    def observe_usage(self, usage: Usage | None) -> None:
        """记下供应商已报告的用量，等待校验成功或失败的终态。"""
        if usage is not None:
            self._call._usage = usage

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """记录尝试的单一终态，并关闭发送资格。"""
        with self._send_lock:
            self._closed = True
        status = _outcome(exc, self._completed)
        usage = self._call._usage
        self._call._emit(
            f"model.attempt.{status}",
            **self._details,
            status=status,
            duration_seconds=time.monotonic() - self._started,
            http_requests=1 if self._sent else None,
            usage=usage.model_dump() if usage is not None else None,
            **_failure(exc),
        )


def record_model_http_request() -> None:
    """让传输钩子在发送前记录绑定的尝试，普通原生 SDK 调用可独立使用。"""
    attempt = _bound_attempt.get()
    if attempt is not None:
        attempt.wire_request()


def record_model_usage(usage: Usage) -> None:
    """在本地输出校验之前记录当前尝试已经返回的供应商用量。"""
    attempt = _bound_attempt.get()
    if attempt is not None:
        attempt.observe_usage(usage)
