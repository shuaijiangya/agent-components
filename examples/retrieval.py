"""演示索引原语、上下文扩展和召回候选选择。

候选和 corpus 必须先经过使用方的权限、文档版本过滤。
"""

import argparse
from pathlib import Path

from agent_components.context import ContextExpander
from agent_components.core.contracts import ChunkResult
from agent_components.selection import RetrievalCandidate, deduplicate, mmr_select
from examples._common import emit, output_arg, protect, read_json, run


def main() -> None:
    """运行一个检索相关组件，不隐式计算向量或执行业务 RAG。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("index", "search"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--dimensions", type=int, required=True)
        cmd.add_argument("--table", default="chunks")
        output_arg(cmd)
        if name == "index":
            cmd.add_argument(
                "--records", type=Path, required=True, help="IndexRecord JSONL；向量由使用方计算"
            )
            cmd.add_argument("--batch-size", type=int, default=128)
            cmd.add_argument("--build-fts", action="store_true")
        else:
            cmd.add_argument("--query", type=Path, required=True)
            cmd.add_argument(
                "--document-ids", type=Path, help="JSON 文档 ID 数组；空数组表示不允许任何文档"
            )
            cmd.add_argument("--limit", type=int, default=10)
    context = commands.add_parser("context")
    context.add_argument("input", type=Path, help="已过滤的 ChunkResult JSON")
    context.add_argument("--hits", type=Path, required=True, help="按相关性排序的 ID 数组")
    context.add_argument("--before", type=int, default=1)
    context.add_argument("--after", type=int, default=1)
    context.add_argument("--parent-mode", choices=["none", "append", "replace"], default="none")
    context.add_argument("--max-characters", type=int, default=8000)
    output_arg(context)
    select = commands.add_parser("select")
    select.add_argument("input", type=Path, help="RetrievalCandidate JSON 数组")
    select.add_argument("--method", choices=["dedup", "mmr"], required=True)
    select.add_argument("--query", type=Path)
    select.add_argument("--limit", type=int, default=10)
    select.add_argument("--relevance-weight", type=float, default=0.5)
    select.add_argument("--threshold", type=float, default=0.85)
    output_arg(select)
    args = parser.parse_args()
    if args.command in {"index", "search"} and args.output:
        if args.output.resolve().is_relative_to(args.db.resolve()):
            raise ValueError("JSON output must be outside the LanceDB directory")
    protect(
        args.output,
        *(
            getattr(args, name, None)
            for name in ("input", "hits", "records", "query", "document_ids")
        ),
    )
    if args.command in {"index", "search"}:
        from agent_components.retrieval import IndexRecord, LanceDBIndex

        index = LanceDBIndex(args.db, dimensions=args.dimensions, table_name=args.table)
        if args.command == "index":
            if args.batch_size < 1:
                raise ValueError("batch size must be positive")
            count, batch = 0, []
            with args.records.open(encoding="utf-8-sig") as reader:
                for line in reader:
                    if not line.strip():
                        continue
                    batch.append(IndexRecord.model_validate_json(line))
                    if len(batch) >= args.batch_size:
                        index.upsert(batch)
                        count += len(batch)
                        batch.clear()
                index.upsert(batch)
                count += len(batch)
            if args.build_fts:
                index.build_fts()
            emit({"upserted": count}, args.output)
        else:
            result = index.search_vector(
                read_json(args.query),
                limit=args.limit,
                document_ids=read_json(args.document_ids) if args.document_ids else None,
            )
            emit([row.model_dump(mode="json") for row in result], args.output)
    elif args.command == "context":
        corpus = ChunkResult.model_validate(read_json(args.input)).chunks
        by_id = {chunk.id: chunk for chunk in corpus}
        hits = [by_id[key] for key in read_json(args.hits)]
        emit(
            ContextExpander().expand(
                hits,
                corpus,
                before=args.before,
                after=args.after,
                parent_mode=args.parent_mode,
                max_characters=args.max_characters,
            ),
            args.output,
        )
    else:
        candidates = [RetrievalCandidate.model_validate(row) for row in read_json(args.input)]
        if args.method == "mmr":
            if not args.query:
                parser.error("mmr requires --query")
            result = mmr_select(
                candidates,
                read_json(args.query),
                limit=args.limit,
                relevance_weight=args.relevance_weight,
            )
        else:
            result = deduplicate(candidates, threshold=args.threshold)
        emit(result, args.output)


if __name__ == "__main__":
    run(main)
