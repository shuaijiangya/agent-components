"""提供确定性的结构和标准检索指标。

评测不自动调用裁判模型，也不执行目标业务流程。
"""

from collections.abc import Sequence
from typing import cast

from pydantic import Field, JsonValue

from agent_components.core.contracts import Contract, Issue
from agent_components.core.errors import MissingDependencyError
from agent_components.core.schema import Schema, normalize_schema, validate_data


class SchemaEvaluation(Contract):
    """返回 Schema 评测结论和定位问题。

    passed 仅表示结构验证通过，不证明内容事实正确。
    """

    passed: bool
    errors: tuple[Issue, ...]


class RelevanceLabel(Contract):
    """声明检索评测的查询、条目和相关性等级。

    relevance 为非负整数，零表示不相关。
    """

    query_id: str
    item_id: str
    relevance: int = Field(ge=0)


class RankedResult(Contract):
    """声明一个查询的检索条目和排序分值。

    分值必须有限；同一查询中不允许重复条目。
    """

    query_id: str
    item_id: str
    score: float = Field(allow_inf_nan=False)


def evaluate_schema(data: JsonValue, schema: Schema) -> SchemaEvaluation:
    """对数据执行 JSON Schema 评测。

    Args:
        data: 待检查的 JSON 值。
        schema: JSON Schema 或 Pydantic 类型，后者转换为 JSON Schema。

    Returns:
        验证结论及带 JSON Pointer 的问题，不运行 Pydantic 自定义 validator。
    """
    errors = validate_data(data, normalize_schema(schema))
    return SchemaEvaluation(passed=not errors, errors=errors)


def evaluate_retrieval(
    labels: Sequence[RelevanceLabel], results: Sequence[RankedResult], *, k: int = 10
) -> dict[str, float]:
    """使用 ir_measures 计算 Recall、nDCG 和 RR。

    Args:
        labels: 非空人工或外部评定的相关性标签。
        results: 带查询身份的排序结果。
        k: 指标截止位置，必须为正数。

    Returns:
        指标名称到聚合分值的映射。

    Raises:
        ValueError: k 非正、标签缺失或结果含重复查询/条目。
        MissingDependencyError: 未安装 evaluation extra。
    """
    if k < 1 or not labels:
        raise ValueError("k must be positive and relevance labels must be provided")
    if len({(r.query_id, r.item_id) for r in results}) != len(results):
        raise ValueError("Duplicate query/item results are not allowed")
    try:
        import ir_measures as ir
    except ImportError as exc:
        raise MissingDependencyError("evaluation") from exc
    qrels = [ir.Qrel(row.query_id, row.item_id, row.relevance) for row in labels]
    run = [ir.ScoredDoc(row.query_id, row.item_id, row.score) for row in results]
    scores = ir.calc_aggregate([ir.R @ k, ir.nDCG @ k, ir.RR @ k], qrels, run)
    return {str(metric): cast(float, value) for metric, value in scores.items()}
