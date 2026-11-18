"""索引规范文本到 Docling 元素、页码和矩形的映射。

按结束位置二分定位候选映射；空白分隔区域仍可定位规范文本，不伪造文件来源。
"""

from bisect import bisect_right
from itertools import islice

from agent_components.core.contracts import Document, SourceRef


class SourceMapper:
    """为一份已校验 Document 创建可复用的位置索引。

    文本范围保持精确；原文件来源保留后端提供的 element/page 精度。
    """

    def __init__(self, document: Document) -> None:
        """预建映射结束位置索引。

        Args:
            document: 结构范围已通过契约校验的文档。
        """
        self._document = document
        self._ends = tuple(item.end for item in document.source_map)

    def resolve(self, start: int, end: int) -> tuple[SourceRef, ...]:
        """读取与给定原文区间相交的来源。

        Args:
            start: 规范文本中的包含端点。
            end: 规范文本中的排除端点。

        Returns:
            精确文本引用及相交元素引用；不插值推算页面局部坐标。

        Raises:
            ValueError: 区间不在文档范围内。
        """
        doc = self._document
        if not 0 <= start < end <= len(doc.text):
            raise ValueError("source lookup must be inside document")
        refs = [
            SourceRef(
                input_id=doc.id,
                document_id=doc.id,
                revision_id=doc.revision_id,
                start=start,
                end=end,
            )
        ]
        index = bisect_right(self._ends, start)
        for span in islice(doc.source_map, index, None):
            if span.start >= end:
                break
            for ref in span.refs:
                # start/end 始终表示规范文本；原生 bbox 保持元素级，不冒充字符级精度。
                refs.append(
                    ref.model_copy(
                        update={"start": max(start, span.start), "end": min(end, span.end)}
                    )
                )
        return tuple(dict.fromkeys(refs))
