"""演示对真实 TCP 就绪探测应用并发、总超时和显式重试。

探测只建立和关闭连接；不要据此推断业务写操作可以安全重试。
"""

import argparse
import asyncio

from agent_components.core.errors import RetryableError
from agent_components.execution import ExecutionController, ExecutionPolicy
from examples._common import emit, output_arg, run


async def execute(args: argparse.Namespace) -> None:
    """共享一个控制器限制并行探测，取消时清理其他任务。"""
    controller = ExecutionController(
        ExecutionPolicy(
            timeout_seconds=args.timeout,
            max_concurrency=args.concurrency,
            max_attempts=args.max_attempts,
        )
    )

    async def probe() -> dict[str, str]:
        try:
            _, writer = await asyncio.open_connection(args.host, args.port)
        except OSError as exc:
            # 只有连接建立前的瞬时故障被显式标记；没有业务写入和隐含重试。
            raise RetryableError("TCP endpoint unavailable") from exc
        try:
            return {"status": "reachable"}
        finally:
            writer.close()
            await writer.wait_closed()

    tasks = [asyncio.create_task(controller.run(probe)) for _ in range(args.probes)]
    try:
        emit(await asyncio.gather(*tasks), args.output)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    """验证用户提供的资源上限后运行网络探测。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--probes", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=1)
    output_arg(parser)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.probes <= 1000:
        parser.error("port must be 1..65535 and probes 1..1000")
    asyncio.run(execute(args))


if __name__ == "__main__":
    run(main)
