"""演示普通聊天、结构化抽取、Embedding 和重排的两种消费方式。

网络请求只在执行命令时发起。示例使用虚构文本，密钥由运行环境提供。
"""

import argparse
import asyncio
import json
import math
import os
from collections.abc import AsyncGenerator, Iterable, Iterator, Sequence
from contextlib import AsyncExitStack, aclosing, closing
from itertools import batched
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_components.core.contracts import Document, ExtractionOptions, ExtractionResult
from agent_components.extraction import Extractor
from agent_components.models import (
    ChatRequest,
    Message,
    ModelClient,
    ModelRequest,
    StateConversationMemory,
    TextDelta,
)
from agent_components.models.providers.openai_compatible import (
    OpenAICompatibleEmbeddings,
    OpenAIModelSettings,
    openai_compatible_clients,
)
from agent_components.models.providers.reranking import HTTPReranker
from examples._common import protect_sqlite, run
from examples._models import configure_logs

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


async def extract_once(
    client: ModelClient, model_alias: str | None, system_prompt: str | None
) -> ExtractionResult:
    """走 SDK 的 Document → ModelClient → Schema 校验链路。"""
    return await Extractor(client).extract(
        CONTRACT,
        ContractAmount,
        ExtractionOptions(model_alias=model_alias, system_prompt=system_prompt),
    )


async def extract_stream(
    client: ModelClient, model_alias: str | None, system_prompt: str | None
) -> AsyncGenerator[dict[str, Any], None]:
    """通过统一客户端选模，预览 JSON 分片并输出业务校验后的结果。"""
    request = ModelRequest(
        messages=(
            Message(role="system", text="只提取原文明确给出的合同金额，单位为人民币元。"),
            Message(role="user", text=CONTRACT.text),
        ),
        schema=ContractAmount.model_json_schema(),
        model_alias=model_alias,
        system_prompt=system_prompt,
        max_tokens=256,
    )
    async with aclosing(client.stream_generate(request)) as events:
        async for event in events:
            if isinstance(event, TextDelta):
                yield {"event": "json_delta", "text": event.text}
            else:
                data = ContractAmount.model_validate(event.data, strict=True)
                yield {"event": "validated", "data": data.model_dump()}


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
    async with AsyncExitStack() as stack:
        memory = None
        if args.memory_db is not None:
            from agent_components.storage.sqlite import SQLiteStateStore

            protect_sqlite(args.memory_db, args.config)
            store = await stack.enter_async_context(
                SQLiteStateStore(args.memory_db, create_tables=True)
            )
            memory = StateConversationMemory(store)
        client, _ = await stack.enter_async_context(
            openai_compatible_clients(settings, memory=memory)
        )
        if args.operation == "extract":
            if args.stream:
                async with aclosing(
                    extract_stream(client, args.model_alias, args.system_prompt)
                ) as events:
                    async for event in events:
                        _emit(event)
                return 0
            result = await extract_once(client, args.model_alias, args.system_prompt)
            _emit(result.model_dump(mode="json"))
            return int(result.status != "success")

        request = ChatRequest(
            messages=(Message(role="user", text=args.prompt or CHAT_PROMPT),),
            model_alias=args.model_alias,
            system_prompt=args.system_prompt,
            session_id=args.session_id,
            max_tokens=256 if args.stream else 128,
        )
        if args.stream:
            async with aclosing(client.stream_chat(request)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        _emit({"event": "text_delta", "text": event.text})
                    else:
                        _emit({"event": "completed"})
        else:
            _emit({"text": (await client.chat(request)).text})
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
    parser.add_argument("--system-prompt", help="覆盖单次文本请求的默认系统提示词")
    parser.add_argument("--prompt", help="chat 本轮用户消息")
    parser.add_argument("--session-id", help="chat 使用的会话 ID")
    parser.add_argument("--memory-db", type=Path, help="chat 的 SQLite 历史库")
    args = parser.parse_args()
    if args.session_id is not None or args.memory_db is not None:
        if args.operation != "chat" or args.session_id is None or args.memory_db is None:
            parser.error("--session-id and --memory-db must be used together with chat")
    if args.operation in ("chat", "extract"):
        configure_logs()
        return asyncio.run(_run_text(args))
    return _run_retrieval_models(args)


if __name__ == "__main__":
    run(main)
