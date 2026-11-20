"""演示普通聊天、结构化抽取、Embedding 和重排的两种消费方式。

网络请求只在执行命令时发起。示例使用虚构文本，密钥由运行环境提供。
"""

import argparse
import asyncio
import json
import math
import os
from collections.abc import AsyncGenerator, Iterable, Iterator, Sequence
from contextlib import aclosing, closing
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from agent_components.core.contracts import Document, ExtractionOptions, ExtractionResult
from agent_components.extraction import Extractor
from agent_components.models import ModelClient
from agent_components.models.providers.openai_compatible import (
    OpenAICompatibleEmbeddings,
    OpenAIModelSettings,
    openai_compatible_clients,
)
from agent_components.models.providers.reranking import HTTPReranker
from examples._common import run
from examples._models import configure_logs

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionMessageParam

BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL = "Pro/BAAI/bge-reranker-v2-m3"
CONTRACT = Document(
    id="demo-contract", text="合同总金额为人民币32000元。验收通过后30日内支付尾款。"
)
CHAT_PROMPT = "用一句话概括这段虚构合同：验收通过后30日内支付尾款。"
PASSAGES = (CONTRACT.text, "员工请假应提前提交申请。")
QUERIES = ("尾款什么时候支付？", "请假需要做什么？")


class ContractAmount(BaseModel):
    """样例中的严格业务结构，金额单位为人民币元。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    total_amount_cny: int = Field(ge=0)


def _emit(event: dict[str, Any]) -> None:
    """输出一行 JSON。流式消费时及时刷新 stdout。"""
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


async def chat_once(model: Any, prompt: str) -> str:
    """用 AgentScope 原生聊天客户端获得完整文本响应。"""
    from agentscope.message import TextBlock, UserMsg

    response = await model([UserMsg(name="user", content=prompt)], max_tokens=128)
    return "".join(block.text for block in response.content if isinstance(block, TextBlock))


async def stream_text(
    client: "AsyncOpenAI",
    model: str,
    messages: list["ChatCompletionMessageParam"],
    *,
    json_mode: bool = False,
) -> AsyncGenerator[str, None]:
    """逐段接收 Chat Completions SSE，可启用 JSON 对象输出。

    流正常结束才返回。超时、截断和取消由调用方处理。
    """
    options = {"response_format": {"type": "json_object"}} if json_mode else {}
    stream = await client.chat.completions.create(
        model=model, messages=messages, stream=True, max_tokens=256, **options
    )
    finished = None
    async with stream:
        async for chunk in stream:
            for choice in chunk.choices:
                if choice.index != 0:
                    continue
                if choice.delta.content:
                    yield choice.delta.content
                if choice.finish_reason is not None:
                    finished = choice.finish_reason
    # 结束事件缺失或因长度停止时，不能把已经收到的片段当成完整结果。
    if finished != "stop":
        raise RuntimeError("Chat stream did not complete normally")


async def extract_once(client: ModelClient, model_alias: str | None) -> ExtractionResult:
    """走 SDK 的 Document → ModelClient → Schema 校验链路。"""
    return await Extractor(client).extract(
        CONTRACT, ContractAmount, ExtractionOptions(model_alias=model_alias)
    )


async def extract_stream(client: "AsyncOpenAI", model: str) -> AsyncGenerator[dict[str, Any], None]:
    """逐段输出 JSON 预览，完成后输出经过严格校验的业务对象。"""
    schema = json.dumps(ContractAmount.model_json_schema(), ensure_ascii=False)
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": f"从给定文本提取合同金额。只返回符合此 JSON Schema 的 JSON 对象：{schema}",
        },
        {"role": "user", "content": CONTRACT.text},
    ]
    fragments = []
    async with aclosing(stream_text(client, model, messages, json_mode=True)) as deltas:
        async for delta in deltas:
            fragments.append(delta)
            yield {"event": "json_delta", "text": delta}

    result = ContractAmount.model_validate_json("".join(fragments), strict=True)
    yield {"event": "validated", "data": result.model_dump()}


def embed_once(
    provider: OpenAICompatibleEmbeddings, texts: Sequence[str]
) -> Sequence[Sequence[float]]:
    """提交一批文本，核验回传向量与索引约定的维度一致。"""
    vectors = provider.embed_batch(texts)
    if len(vectors) != len(texts) or any(
        len(vector) != provider.dimension
        or not all(math.isfinite(value) for value in vector)
        or not any(vector)
        for vector in vectors
    ):
        raise ValueError("Invalid embedding count, dimension or values")
    return vectors


def stream_embedding_batches(
    provider: OpenAICompatibleEmbeddings, texts: Iterable[str], *, batch_size: int
) -> Iterator[tuple[int, Sequence[Sequence[float]]]]:
    """每取一批就计算一批，返回该批向量和它在输入中的起始位置。"""
    for number, batch in enumerate(batched(texts, batch_size)):
        yield number * batch_size, embed_once(provider, batch)


def rerank_once(
    provider: HTTPReranker, query: str, passages: Sequence[str]
) -> list[dict[str, Any]]:
    """将同一查询的完整候选集送去重排，按模型分数降序返回。"""
    scores = sorted(provider.rerank(query, passages), key=lambda item: item.score, reverse=True)
    return [
        {"index": item.index, "score": item.score, "text": passages[item.index]} for item in scores
    ]


def stream_rankings(
    provider: HTTPReranker, queries: Iterable[str], passages: Sequence[str]
) -> Iterator[dict[str, Any]]:
    """每次取一个查询并返回它的完整排名。"""
    for query in queries:
        yield {"query": query, "ranking": rerank_once(provider, query, passages)}


async def _run_text(args: argparse.Namespace) -> int:
    settings = OpenAIModelSettings.model_validate_json(args.config.read_bytes())
    async with openai_compatible_clients(settings) as (client, native):
        if args.operation == "extract" and not args.stream:
            result = await extract_once(client, args.model_alias)
            _emit(result.model_dump(mode="json"))
            return int(result.status != "success")

        alias = args.model_alias or settings.default_alias
        model = native[alias]
        if args.operation == "extract":
            async with aclosing(extract_stream(model.client, model.model)) as events:
                async for event in events:
                    _emit(event)
        elif args.stream:
            messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": CHAT_PROMPT}]
            async with aclosing(stream_text(model.client, model.model, messages)) as deltas:
                async for delta in deltas:
                    _emit({"event": "text_delta", "text": delta})
            _emit({"event": "completed"})
        else:
            _emit({"text": await chat_once(model, CHAT_PROMPT)})
    return 0


def _run_retrieval_models(args: argparse.Namespace) -> int:
    base_url = os.environ.get("SILICONFLOW_BASE_URL", BASE_URL).rstrip("/")
    if args.operation == "embed":
        provider = OpenAICompatibleEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=os.environ["SILICONFLOW_API_KEY"],
            base_url=base_url,
            dimension=1024,
        )
        with closing(provider):
            if args.stream:
                batches = stream_embedding_batches(provider, PASSAGES, batch_size=1)
            else:
                batches = iter(((0, embed_once(provider, PASSAGES)),))
            for offset, vectors in batches:
                _emit({"start": offset, "dimension": provider.dimension, "vectors": vectors})
    else:
        reranker = HTTPReranker(
            endpoint_url=f"{base_url}/rerank",
            model=RERANK_MODEL,
            api_key_env="SILICONFLOW_API_KEY",
        )
        if args.stream:
            for event in stream_rankings(reranker, QUERIES, PASSAGES):
                _emit(event)
        else:
            _emit({"query": QUERIES[0], "ranking": rerank_once(reranker, QUERIES[0], PASSAGES)})
    return 0


def main() -> int:
    """选择一种样例执行。文本 SSE 与批处理逐条输出 JSONL。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("chat", "extract", "embed", "rerank"))
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("examples/data/models.json"))
    parser.add_argument("--model-alias", help="为 chat 或 extract 指定已配置的模型别名")
    args = parser.parse_args()
    if args.operation in ("chat", "extract"):
        configure_logs()
        return asyncio.run(_run_text(args))
    return _run_retrieval_models(args)


if __name__ == "__main__":
    run(main)
