"""实现可注入的 Token 计量、句子边界和原生 Chonkie 语义策略。

模型与分词器由使用方提供；不根据模型名隐式下载权重，也不把字符数冒充 Token。
"""

import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from agent_components.core.errors import ChunkAlignmentError, MissingDependencyError


class TokenEncoder(Protocol):
    """约定真实 Token 编码器的最小能力。

    可直接传入已加载的 tiktoken、Hugging Face tokenizer 或兼容对象。
    """

    def encode(self, text: str) -> Sequence[int]:
        """编码文本，不自动加入特殊 Token。

        Args:
            text: 待编码的原文。

        Returns:
            实际 Token ID 序列。
        """
        ...


class EmbeddingProvider(Protocol):
    """约定由调用方配置的批量向量能力。

    dimension 必须固定；输出必须是非零、有限且维度一致的向量。
    """

    dimension: int

    def embed_batch(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """批量计算文本向量。

        Args:
            texts: 原文片段的有序序列。

        Returns:
            与输入一一对应的向量。
        """
        ...


def literal_spans(text: str, delimiters: Sequence[str], include: str) -> list[tuple[int, int]]:
    """保留字面分隔符并返回原文区间。

    Args:
        text: 完整待切文本。
        delimiters: 非空字面量序列，不执行调用方的正则。
        include: previous 归前块，next 归后块。

    Returns:
        无丢失、无重叠的非空范围。
    """
    pattern = re.compile("|".join(re.escape(d) for d in sorted(delimiters, key=len, reverse=True)))
    boundaries = [0]
    for match in pattern.finditer(text):
        point = match.end() if include == "previous" else match.start()
        if point > boundaries[-1]:
            boundaries.append(point)
    if boundaries[-1] != len(text):
        boundaries.append(len(text))
    return list(zip(boundaries[:-1], boundaries[1:], strict=True))


def token_count(encoder: TokenEncoder, text: str) -> int:
    """读取实际 Token 数并拒绝非空文本的空编码。

    Args:
        encoder: 使用方注入的编码器。
        text: 原文片段。

    Returns:
        实际编码长度。

    Raises:
        ValueError: 非空文本被编码为空。
    """
    count = len(encoder.encode(text))
    if text and count == 0:
        raise ValueError("tokenizer produced no tokens for nonempty text")
    return count


def token_spans(text: str, size: int, encoder: TokenEncoder) -> list[tuple[int, int]]:
    """在原文字符边界寻找满足真实 Token 预算的窗口。

    Args:
        text: 待切文本。
        size: 每片实际 Token 上限。
        encoder: 已配置的真实编码器。

    Returns:
        可完整还原原文的区间；不解码截断 Token，避免生成替换字符。

    Raises:
        ValueError: 当前预算无法容纳待切的单个 Python 字符。
    """
    result = []
    start = 0
    while start < len(text):
        # 仅探测有界字符窗口；BPE 的计数不严格单调，最终总是复验所选切片。
        high = min(len(text), start + max(64, size * 16))
        if token_count(encoder, text[start:high]) <= size:
            end = high
        else:
            low, upper = start + 1, high
            end = start
            while low <= upper:
                mid = (low + upper) // 2
                if token_count(encoder, text[start:mid]) <= size:
                    end, low = mid, mid + 1
                else:
                    upper = mid - 1
            if end == start:
                raise ValueError("token budget cannot fit a character; increase size")
        result.append((start, end))
        start = end
    return result


def sentence_spans(
    text: str,
    size: int,
    delimiters: Sequence[str],
    sentences_per_chunk: int,
) -> list[tuple[int, int]]:
    """使用 Chonkie 的句子边界逻辑并限制每块句子数量。

    Args:
        text: 待切原文。
        size: 硬字符上限，超长单句会继续切分。
        delimiters: 句末字面分隔符。
        sentences_per_chunk: 每块最多句子数。

    Returns:
        连续、保留空白的原文区间。

    Raises:
        MissingDependencyError: 未安装 chunking extra。
        ChunkAlignmentError: 原生句子切分未完整保留原文。
    """
    try:
        from chonkie.chunker.base import split_text_by_delimiters
    except ImportError as exc:
        raise MissingDependencyError("chunking") from exc
    sentences = split_text_by_delimiters(
        text, delimiters=list(delimiters), include_delim="prev", min_chars=1
    )
    if "".join(sentences) != text:
        raise ChunkAlignmentError("sentence splitting did not preserve original text")
    spans = []
    position = 0
    for offset in range(0, len(sentences), sentences_per_chunk):
        length = sum(len(s) for s in sentences[offset : offset + sentences_per_chunk])
        for start in range(position, position + length, size):
            spans.append((start, min(start + size, position + length)))
        position += length
    return spans


def semantic_spans(
    text: str,
    size: int,
    embedding: EmbeddingProvider,
    *,
    threshold: float,
    similarity_window: int,
    delimiters: Sequence[str],
    batch_size: int,
    window_chars: int,
) -> list[tuple[int, int]]:
    """桥接注入向量到 Chonkie SemanticChunker，并复验连续原文。

    Args:
        text: 待切原文。
        size: 最终字符上限。
        embedding: 调用方提供的向量能力。
        threshold: 原生语义分界阈值。
        similarity_window: 原生前文比较窗口。
        delimiters: 句末边界。
        batch_size: 每次向量请求的最大文本数。
        window_chars: 单次语义分析最多处理的原文字符数。

    Returns:
        语义分组的原文范围，超长组按字符上限继续拆分。

    Raises:
        MissingDependencyError: 未安装 chunking extra。
        ChunkAlignmentError: 原生分组无法逐段还原原文。
        ValueError: 向量数目、维度或有限性不符合契约。
    """
    try:
        import numpy as np
        from chonkie import SemanticChunker
        from chonkie.embeddings import BaseEmbeddings
    except ImportError as exc:
        raise MissingDependencyError("chunking") from exc

    class Bridge(BaseEmbeddings):  # type: ignore[misc]
        """把显式注入的批量向量能力转换为 Chonkie 原生接口。

        只使用已提供对象，不调用 AutoEmbeddings 或任何模型下载入口。
        """

        @property
        def dimension(self) -> int:
            """读取注入提供方的固定维度。

            Returns:
                向量维度。
            """
            return embedding.dimension

        def get_tokenizer(self) -> str:
            """使原生语义算法使用字符预算。

            Returns:
                原生 character tokenizer 名称。
            """
            return "character"

        def embed(self, text: str) -> Any:
            """计算单条向量。

            Args:
                text: 原生算法需要比较的文本。

            Returns:
                经校验的 NumPy 向量。
            """
            return self.embed_batch([text])[0]

        def embed_batch(self, texts: list[str]) -> list[Any]:
            """以有界批次调用提供方并校验向量。

            Args:
                texts: 原生算法生成的窗口或句子。

            Returns:
                有限、非零、维度一致的 NumPy 向量。
            """
            output = []
            for offset in range(0, len(texts), batch_size):
                batch = texts[offset : offset + batch_size]
                vectors = list(embedding.embed_batch(batch))
                if len(vectors) != len(batch):
                    raise ValueError("embedding result count differs from input")
                for vector in vectors:
                    if (
                        len(vector) != embedding.dimension
                        or len(vector) == 0
                        or not all(math.isfinite(v) for v in vector)
                        or not any(v != 0 for v in vector)
                    ):
                        raise ValueError("invalid semantic embedding vector")
                    output.append(np.asarray(vector, dtype=float))
            return output

    native = SemanticChunker(
        embedding_model=Bridge(),
        threshold=threshold,
        chunk_size=size,
        similarity_window=similarity_window,
        min_characters_per_sentence=1,
        delim=list(delimiters),
        skip_window=0,
        filter_window=5,
        filter_polyorder=3,
    )
    spans = []
    for origin in range(0, len(text), window_chars):
        window = text[origin : origin + window_chars]
        if window.isspace():
            chunks = [window]
        else:
            chunks = [item.text for item in native.chunk(window)]
        if "".join(chunks) != window:
            raise ChunkAlignmentError("semantic grouping did not preserve original text")
        position = origin
        for value in chunks:
            for start in range(position, position + len(value), size):
                spans.append((start, min(start + size, position + len(value))))
            position += len(value)
    return spans
