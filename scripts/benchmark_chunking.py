"""记录大文本流式资源开销和共享组件的线程确定性。

不调用模型，不把本机数据当作生产 SLA；结果包含原始参数和运行环境。
"""

import argparse
import hashlib
import json
import platform
import resource
import tempfile
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_components.chunking import ChunkPolicy
from agent_components.streaming import StreamingChunker


def write_text(path: Path, characters: int) -> None:
    """生成带中文、Emoji 和 CRLF 的可重复文本文件。

    Args:
        path: 输出路径。
        characters: 精确的 Python 字符数。
    """
    pattern = "合同条款、付款金额与验收义务。🙂\r\n"
    block = pattern * 1024
    with path.open("w", encoding="utf-8", newline="") as writer:
        remaining = characters
        while remaining:
            value = block[:remaining]
            writer.write(value)
            remaining -= len(value)


def consume(path: Path, expected_characters: int) -> dict[str, int | str | float]:
    """消费产物且不保留块集合，校验长度和确定性摘要。

    Args:
        path: 规范文本路径。
        expected_characters: 预期的规范文本字符数。

    Returns:
        块数、覆盖长度、块 ID 摘要和耗时。
    """
    component = StreamingChunker(window_chars=65536)
    policy = ChunkPolicy(strategy="character", size=1000)
    digest = hashlib.sha256()
    cursor = 0
    count = 0
    started = time.perf_counter()
    for chunk in component.iter_file(path, policy, document_id="benchmark"):
        if chunk.start != cursor or len(chunk.text) > 1000:
            raise AssertionError("chunk position or budget mismatch")
        cursor = chunk.end
        count += 1
        digest.update(chunk.id.encode())
    if cursor != expected_characters:
        raise AssertionError("document was not fully covered")
    return {
        "chunks": count,
        "covered_characters": cursor,
        "id_digest": digest.hexdigest(),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }


def main() -> None:
    """运行带资源记录的大文件与八线程负载检查。

    输出 JSON 用于复核，不建立绑定特定硬件的测试性能门槛。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--characters", type=int, default=24 * 1024 * 1024)
    parser.add_argument("--parallel-characters", type=int, default=1_000_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--requests", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.characters, args.parallel_characters, args.workers, args.requests) < 1:
        parser.error("resource parameters must be positive")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "large.txt"
        write_text(path, args.characters)
        tracemalloc.start()
        large = consume(path, args.characters)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        large["peak_traced_python_bytes"] = peak
        large["input_utf8_bytes"] = path.stat().st_size
        path = Path(directory) / "parallel.txt"
        write_text(path, args.parallel_characters)
        baseline = consume(path, args.parallel_characters)
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            results = list(
                executor.map(
                    lambda _: consume(path, args.parallel_characters), range(args.requests)
                )
            )
        elapsed = time.perf_counter() - started
        if not all(result["id_digest"] == baseline["id_digest"] for result in results):
            raise AssertionError("concurrent output differs from baseline")
        output = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "strategy": "character",
            "size": 1000,
            "window_chars": 65536,
            "large_file": large,
            "parallel": {
                "workers": args.workers,
                "requests": args.requests,
                "characters_per_request": args.parallel_characters,
                "elapsed_seconds": round(elapsed, 6),
                "all_ids_match_baseline": True,
            },
            "process_peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "scope": "local resource and determinism checks; not production SLA or model accuracy",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
