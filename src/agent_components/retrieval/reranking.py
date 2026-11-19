"""定义重排端口及 Cohere 兼容的 HTTP 适配器。"""

import json
import math
import os
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection, IncompleteRead
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

from agent_components.core.errors import ComponentError

_FailureReason = Literal["configuration", "timeout", "transport", "http_status", "invalid_response"]
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_FAILURE_MESSAGES = {
    "configuration": "Invalid reranker configuration or credentials",
    "timeout": "Reranker request timed out",
    "transport": "Unable to complete reranker HTTP request",
    "http_status": "Reranker endpoint returned an unsuccessful HTTP status",
    "invalid_response": "Reranker endpoint returned invalid or incomplete scores",
}


class RerankerError(ComponentError):
    """表示重排失败，错误信息省略凭据、请求数据和远端正文。

    reason 标记配置、超时、传输、HTTP 状态或响应格式问题。
    status_code 在 HTTP 状态失败时提供远端状态码。
    """

    code = "reranker_error"

    def __init__(self, reason: _FailureReason, *, status_code: int | None = None) -> None:
        """以固定消息报告失败类型及可用的 HTTP 状态。"""
        self.reason = reason
        self.status_code = status_code
        message = _FAILURE_MESSAGES[reason]
        if status_code is not None:
            message = f"{message} ({status_code})"
        super().__init__(message)


@dataclass(frozen=True)
class RerankScore:
    """记录输入的零基索引及有限相关度分数，分数越高越相关。"""

    index: int
    score: float


class RerankerProvider(Protocol):
    """约定对同批候选文本计算相关度的同步接口。"""

    def rerank(self, query: str, texts: Sequence[str]) -> tuple[RerankScore, ...]:
        """按输入索引顺序返回每项候选的分数。"""
        ...


class HTTPReranker:
    """向调用方指定的 Cohere 兼容端点发送重排请求。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        model: str,
        api_key: str | SecretStr | None = None,
        api_key_env: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        """配置 HTTP 端点、模型和可选的 Bearer 认证。

        Args:
            endpoint_url: 包含路由路径的完整 HTTP 或 HTTPS URL。
            model: 请求发送的模型名称。
            api_key: 直接提供的 Bearer 密钥，支持字符串或 SecretStr。
            api_key_env: 每次请求前读取密钥的环境变量名。与 api_key 互斥，
                两者都省略时发送无认证请求。
            timeout: 连接和套接字 I/O 的超时秒数，默认 10 秒。

        Raises:
            RerankerError: 端点、模型、超时或认证配置无效。
        """
        self._scheme, self._host, self._port, self._target = _parse_endpoint(endpoint_url)
        if not model.strip() or isinstance(timeout, bool) or not 0 < timeout < math.inf:
            raise RerankerError("configuration")
        if api_key_env is not None and (
            api_key is not None or not api_key_env or "=" in api_key_env or "\x00" in api_key_env
        ):
            raise RerankerError("configuration")
        self._model = model
        self._timeout = timeout
        self._api_key = SecretStr(api_key) if isinstance(api_key, str) else api_key
        self._api_key_env = api_key_env

    def rerank(self, query: str, texts: Sequence[str]) -> tuple[RerankScore, ...]:
        """提交查询和所有候选，并按输入索引顺序返回经验证的分数。

        候选为空时直接返回空结果。端点故障以 RerankerError 报告。
        """
        documents = list(texts)
        if not documents:
            return ()

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        authorization = self._authorization()
        if authorization is not None:
            headers["Authorization"] = authorization
        body = json.dumps(
            {"model": self._model, "query": query, "documents": documents, "top_n": len(documents)}
        ).encode("utf-8")
        connection_type = HTTPSConnection if self._scheme == "https" else HTTPConnection
        port = self._port if self._port is not None else connection_type.default_port
        try:
            with closing(connection_type(self._host, port, timeout=self._timeout)) as client:
                client.request("POST", self._target, body=body, headers=headers)
                with client.getresponse() as response:
                    if not 200 <= response.status < 300:
                        raise RerankerError("http_status", status_code=response.status)
                    if response.length is not None and response.length > _MAX_RESPONSE_BYTES:
                        raise RerankerError("invalid_response")
                    # 也约束无 Content-Length 的响应，避免对异常端点进行无界读取。
                    response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(response_body) > _MAX_RESPONSE_BYTES or response.length:
                        raise RerankerError("invalid_response")
        except TimeoutError:
            reason: _FailureReason = "timeout"
        except IncompleteRead:
            reason = "invalid_response"
        except OverflowError:
            reason = "configuration"
        except (OSError, HTTPException):
            reason = "transport"
        else:
            return _parse_scores(response_body, len(documents))
        raise RerankerError(reason)

    def _authorization(self) -> str | None:
        if self._api_key is not None:
            token = self._api_key.get_secret_value()
        elif self._api_key_env is not None:
            token = os.environ.get(self._api_key_env, "")
        else:
            return None
        if not token or any(not 32 < ord(character) < 127 for character in token):
            raise RerankerError("configuration")
        return f"Bearer {token}"


def _parse_endpoint(url: str) -> tuple[str, str, int | None, str]:
    if "#" in url or any(not 32 < ord(character) < 127 for character in url):
        raise RerankerError("configuration")
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError:
        raise RerankerError("configuration") from None
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username is not None
    ):
        raise RerankerError("configuration")
    target = endpoint.path or "/"
    if endpoint.query:
        target = f"{target}?{endpoint.query}"
    return endpoint.scheme, endpoint.hostname, port, target


def _parse_scores(body: bytes, count: int) -> tuple[RerankScore, ...]:
    try:
        payload: object = json.loads(body)
    except (ValueError, RecursionError):
        payload = None
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != count:
        raise RerankerError("invalid_response")

    scores: dict[int, RerankScore] = {}
    for result in results:
        if not isinstance(result, dict):
            raise RerankerError("invalid_response")
        index = result.get("index")
        score = result.get("relevance_score")
        if type(index) is not int or not 0 <= index < count or index in scores:
            raise RerankerError("invalid_response")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise RerankerError("invalid_response")
        try:
            value = float(score)
        except OverflowError:
            raise RerankerError("invalid_response") from None
        if not math.isfinite(value):
            raise RerankerError("invalid_response")
        scores[index] = RerankScore(index=index, score=value)
    return tuple(scores[index] for index in range(count))
