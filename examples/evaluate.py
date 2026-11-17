"""演示 Schema 校验与标准检索指标，不使用裁判模型。

Schema 通过只表示结构符合；检索指标需要外部标注的相关性标签。
"""

import argparse
from pathlib import Path

from agent_components.evaluation import (
    RankedResult,
    RelevanceLabel,
    evaluate_retrieval,
    evaluate_schema,
)
from examples._common import emit, output_arg, protect, read_json, run


def main() -> int:
    """读取显式评测输入并返回结果，结构不合格时退出码为一。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema")
    schema.add_argument("input", type=Path)
    schema.add_argument("--schema", type=Path, required=True)
    retrieval = commands.add_parser("retrieval")
    retrieval.add_argument("input", type=Path, help="RankedResult JSON 数组")
    retrieval.add_argument("--labels", type=Path, required=True)
    retrieval.add_argument("--k", type=int, default=10)
    output_arg(schema)
    output_arg(retrieval)
    args = parser.parse_args()
    protect(args.output, args.input, getattr(args, "schema", None), getattr(args, "labels", None))
    if args.command == "schema":
        result = evaluate_schema(read_json(args.input), read_json(args.schema))
        emit(result, args.output)
        return int(not result.passed)
    emit(
        evaluate_retrieval(
            [RelevanceLabel.model_validate(row) for row in read_json(args.labels)],
            [RankedResult.model_validate(row) for row in read_json(args.input)],
            k=args.k,
        ),
        args.output,
    )
    return 0


if __name__ == "__main__":
    run(main)
