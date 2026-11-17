"""演示策略路由、结构化提取和 LangExtract Provider 桥接。

只有配置了真实端点并执行命令才会调用模型；结构校验不能证明事实正确。
"""

import argparse
import asyncio
import json
from pathlib import Path

from agent_components.core.contracts import (
    Chunk,
    ChunkResult,
    ExtractionOptions,
    Message,
    ModelRequest,
)
from agent_components.extraction import Extractor
from examples._common import document, emit, input_args, output_arg, protect, read_json, run
from examples._models import clients, configure_logs


async def execute(args: argparse.Namespace) -> int:
    """按调用方式处理输入，保留失败候选并返回明确退出码。"""
    schema = read_json(args.schema)
    async with clients(args.config) as (client, _):
        if args.command == "langextract":
            from agent_components.adapters.langextract import create_provider

            prompts = read_json(args.input)
            if not isinstance(prompts, list) or not all(
                isinstance(value, str) for value in prompts
            ):
                raise ValueError("prompts must be a JSON string array")
            provider = create_provider(
                client,
                loop=asyncio.get_running_loop(),
                output_schema=schema,
                model_alias=args.model_alias,
            )
            # 同步 Provider 必须放在工作线程，所属事件循环继续处理实际异步请求。
            rows = await asyncio.to_thread(lambda: list(provider.infer(prompts)))
            emit([[json.loads(item.output) for item in row] for row in rows], args.output)
            return 0
        if args.input_kind == "chunk":
            content = Chunk.model_validate(read_json(args.input))
        elif args.input_kind == "chunks":
            content = ChunkResult.model_validate(read_json(args.input)).leaves
        else:
            content = document(args.input, args.input_kind, args.document_id)
        if args.command == "extract":
            result = await Extractor(client).extract(
                content,
                schema,
                ExtractionOptions(
                    instructions=args.instructions,
                    model_alias=args.model_alias,
                    request_id=args.request_id,
                ),
            )
            emit(result, args.output)
            return int(result.status != "success")
        if isinstance(content, tuple):
            text = json.dumps(
                [{"input_id": item.id, "text": item.text} for item in content], ensure_ascii=False
            )
        else:
            text = content.text
        response = await client.generate(
            ModelRequest(
                messages=(Message(role="user", text=text),),
                schema=schema,
                task=args.task,
                model_alias=args.model_alias,
                request_id=args.request_id,
            )
        )
        emit(response, args.output)
        return 0


def main() -> int:
    """配置组件并调用模型；供应商异常只输出错误类型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "extract", "langextract"):
        cmd = commands.add_parser(name)
        if name == "langextract":
            cmd.add_argument(
                "input", type=Path, help="提示字符串 JSON 数组；Schema 为 extraction envelope"
            )
        else:
            input_args(cmd, chunks=True)
            cmd.add_argument("--instructions", default="")
            cmd.add_argument("--request-id")
            if name == "generate":
                cmd.add_argument("--task", default="structured_extraction")
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--schema", type=Path, required=True)
        cmd.add_argument("--model-alias")
        output_arg(cmd)
    args = parser.parse_args()
    protect(args.output, args.input, args.config, args.schema)
    configure_logs()
    return asyncio.run(execute(args))


if __name__ == "__main__":
    run(main)
