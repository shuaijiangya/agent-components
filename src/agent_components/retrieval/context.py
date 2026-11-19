"""提供父块补充、相邻块扩展和无重复的上下文组装。

仅在调用方提供的可见语料中查找，不替使用方访问数据库、推断权限或执行流程。
"""

from collections import defaultdict
from collections.abc import Sequence

from agent_components.core.contracts import Chunk, Contract, SourceRef


def _merge_source_intervals(refs: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
    """按相同来源锚点合并重叠区间，保留不同页码、元素、精度或矩形。"""
    grouped: dict[SourceRef, list[tuple[int, int]]] = {}
    unlocated = set()
    for ref in refs:
        key = ref.model_copy(update={"start": None, "end": None})
        ranges = grouped.setdefault(key, [])
        if ref.start is not None and ref.end is not None:
            ranges.append((ref.start, ref.end))
        else:
            unlocated.add(key)
    result = []
    for key, ranges in grouped.items():
        if key in unlocated:
            result.append(key)
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result.extend(key.model_copy(update={"start": start, "end": end}) for start, end in merged)
    return tuple(result)


class ContextWindow(Contract):
    """承载一个文档版本中连续且去除重叠的上下文。

    text 与 [start,end) 对齐；重复表头属于 context_prefix，不改变正文坐标。
    """

    document_id: str
    revision_id: str
    start: int
    end: int
    text: str
    chunk_ids: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    context_prefix: str = ""

    @property
    def index_text(self) -> str:
        """读取带独立上下文前缀的正文。

        Returns:
            重复表头与精确正文的组合。
        """
        return self.context_prefix + self.text


class ContextResult(Contract):
    """记录上下文窗口和因预算未包含的块。

    不截断块正文；超过预算的身份在 omitted_ids 中明确返回。
    """

    windows: tuple[ContextWindow, ...]
    selected_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]


class ContextExpander:
    """在已通过权限与发布版本过滤的块集合中补充上下文。

    相邻查找按文档版本和父块范围隔离，遇到缺失序号立即停止，不跨不可见空隙。
    """

    def expand(
        self,
        hits: Sequence[Chunk],
        corpus: Sequence[Chunk],
        *,
        before: int = 1,
        after: int = 1,
        parent_mode: str = "none",
        stay_in_parent: bool = True,
        max_characters: int = 8000,
        allow_missing_parents: bool = False,
    ) -> ContextResult:
        """补充父块或邻块，按命中顺序应用整体预算。

        Args:
            hits: 检索命中，按相关性排序。
            corpus: 使用方已经过滤权限及版本的块集合，含可选父块。
            before: 每个命中最多向前扩展的叶子数。
            after: 每个命中最多向后扩展的叶子数。
            parent_mode: none 不补父块，append 同时补父块，replace 用父块替换子块。
            stay_in_parent: 默认不跨父块扩展相邻叶子。
            max_characters: 合并后的正文及重复表头总字符上限。
            allow_missing_parents: 父块被权限过滤时保留可见叶子与邻块。

        Returns:
            合并重叠后的窗口、采用的块身份和预算遗漏身份。

        Raises:
            ValueError: 参数无效、命中不在语料中、父关系不合法或重叠正文冲突。
        """
        for value in (before, after):
            if type(value) is not int or value < 0:
                raise ValueError("neighbor counts must be nonnegative integers")
        if type(max_characters) is not int or max_characters < 1:
            raise ValueError("max_characters must be a positive integer")
        if parent_mode not in {"none", "append", "replace"}:
            raise ValueError("invalid parent_mode")
        by_id = {chunk.id: chunk for chunk in corpus}
        if len(by_id) != len(corpus):
            raise ValueError("corpus chunk IDs must be unique")
        leaves = {}
        for chunk in corpus:
            if chunk.kind == "leaf":
                key = (chunk.document_id, chunk.revision_id, chunk.ordinal)
                if key in leaves:
                    raise ValueError("leaf ordinal must be unique within a document version")
                leaves[key] = chunk
        requests = []
        seen = set()
        for hit in hits:
            if by_id.get(hit.id) != hit:
                raise ValueError("hit does not match the visible corpus")
            related = [hit]
            if hit.kind == "leaf":
                for direction, count in ((-1, before), (1, after)):
                    for distance in range(1, count + 1):
                        neighbor = leaves.get(
                            (hit.document_id, hit.revision_id, hit.ordinal + direction * distance)
                        )
                        if neighbor is None or (
                            stay_in_parent and neighbor.parent_id != hit.parent_id
                        ):
                            break
                        related.append(neighbor)
            if parent_mode != "none" and hit.parent_id:
                parent = by_id.get(hit.parent_id)
                if parent is None:
                    if not allow_missing_parents:
                        raise ValueError("parent is missing from visible corpus")
                elif (
                    parent.kind != "parent"
                    or (parent.document_id, parent.revision_id)
                    != (hit.document_id, hit.revision_id)
                    or not parent.start <= hit.start < hit.end <= parent.end
                ):
                    raise ValueError("parent is missing or inconsistent in visible corpus")
                else:
                    related = [parent] if parent_mode == "replace" else related + [parent]
            for chunk in related:
                if chunk.id not in seen:
                    seen.add(chunk.id)
                    requests.append(chunk)
        selected: list[Chunk] = []
        omitted = []
        for chunk in requests:
            windows = self._assemble([*selected, chunk])
            if sum(len(w.index_text) for w in windows) > max_characters:
                omitted.append(chunk.id)
            else:
                selected.append(chunk)
        return ContextResult(
            windows=tuple(
                window.model_copy(
                    update={"source_refs": _merge_source_intervals(window.source_refs)}
                )
                for window in self._assemble(selected)
            ),
            selected_ids=tuple(c.id for c in selected),
            omitted_ids=tuple(omitted),
        )

    def _assemble(self, chunks: Sequence[Chunk]) -> tuple[ContextWindow, ...]:
        """按原文区间合并重叠，验证重复区域的正文一致。

        Args:
            chunks: 已采用的块序列。

        Returns:
            每个文档版本内按位置排序的连续窗口，不填充原文空隙。
        """
        groups: dict[tuple[str, str], list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            groups[(chunk.document_id, chunk.revision_id)].append(chunk)
        result: list[ContextWindow] = []
        for (document_id, revision_id), group in groups.items():
            current: ContextWindow | None = None
            for chunk in sorted(group, key=lambda c: (c.start, -c.end)):
                if current is None or chunk.start > current.end:
                    if current is not None:
                        result.append(current)
                    current = ContextWindow(
                        document_id=document_id,
                        revision_id=revision_id,
                        start=chunk.start,
                        end=chunk.end,
                        text=chunk.text,
                        chunk_ids=(chunk.id,),
                        source_refs=chunk.source_refs,
                        context_prefix=chunk.context_prefix,
                    )
                    continue
                overlap = min(current.end, chunk.end) - chunk.start
                left = chunk.start - current.start
                if current.text[left : left + overlap] != chunk.text[:overlap]:
                    raise ValueError("overlapping chunks have inconsistent canonical text")
                suffix = chunk.text[overlap:] if chunk.end > current.end else ""
                current = current.model_copy(
                    update={
                        "end": max(current.end, chunk.end),
                        "text": current.text + suffix,
                        "chunk_ids": current.chunk_ids + (chunk.id,),
                        "source_refs": tuple(
                            dict.fromkeys((*current.source_refs, *chunk.source_refs))
                        ),
                    }
                )
            if current is not None:
                result.append(current)
        return tuple(result)
