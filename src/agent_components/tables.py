"""兼容原表格识别与切分的导入路径。"""

from agent_components.chunking.tables import SpanPiece, split_table
from agent_components.core.markdown_tables import markdown_table_ranges

__all__ = ["SpanPiece", "markdown_table_ranges", "split_table"]
