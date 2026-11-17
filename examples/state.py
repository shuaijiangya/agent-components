"""演示公共状态封套的 SQLite 乐观并发保存。

建表必须显式开启；线上先迁移，再根据读到的 revision 执行条件更新。
"""

import argparse
import asyncio
from pathlib import Path

from agent_components.core.contracts import StateEnvelope
from examples._common import emit, output_arg, protect, protect_sqlite, read_json, run


async def execute(args: argparse.Namespace) -> None:
    """通过异步上下文释放 SQLite 连接池。"""
    from agent_components.storage.sqlite import SQLiteStateStore

    async with SQLiteStateStore(args.db, create_tables=args.create_tables) as store:
        if args.command == "save":
            result = await store.save(
                args.namespace,
                args.key,
                StateEnvelope.model_validate(read_json(args.state)),
                expected_revision=args.expected_revision,
            )
        else:
            result = await store.load(args.namespace, args.key)
        emit(result, args.output)


def main() -> None:
    """读取或比较并保存一个状态，不自动覆盖冲突版本。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("load", "save"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--namespace", required=True)
        cmd.add_argument("--key", required=True)
        cmd.add_argument("--create-tables", action="store_true")
        output_arg(cmd)
        if name == "save":
            cmd.add_argument("--state", type=Path, required=True)
            cmd.add_argument("--expected-revision", type=int, required=True)
    args = parser.parse_args()
    protect(args.output, getattr(args, "state", None))
    protect_sqlite(args.db, args.output, getattr(args, "state", None))
    asyncio.run(execute(args))


if __name__ == "__main__":
    run(main)
