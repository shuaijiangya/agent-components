"""归一化 JSON Schema 并返回稳定的问题路径。

路径采用 RFC 6901 JSON Pointer，校验不会修改输入 Schema。
"""

from copy import deepcopy
from typing import Any

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import BaseModel, JsonValue

from agent_components.core.contracts import Issue
from agent_components.core.errors import InvalidSchemaError

type Schema = dict[str, JsonValue] | type[BaseModel]


def pointer(parts: Any) -> str:
    """将路径分段转换为 JSON Pointer。

    Args:
        parts: 字段名或数组下标的可迭代序列。

    Returns:
        转义斜线和波浪线后的路径，根位置为空字符串。
    """
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def normalize_schema(schema: Schema) -> dict[str, JsonValue]:
    """归一化并预检目标 Schema。

    Args:
        schema: JSON Schema 字典或 Pydantic 模型类型。

    Returns:
        可独立修改的深拷贝 JSON Schema。

    Raises:
        InvalidSchemaError: Schema 本身不符合所选规范。
    """
    raw = schema.model_json_schema() if isinstance(schema, type) else schema
    try:
        validator_for(raw).check_schema(raw)
    except SchemaError as exc:
        raise InvalidSchemaError(exc.message) from exc
    return deepcopy(dict(raw))


def validate_data(data: JsonValue, schema: dict[str, JsonValue]) -> tuple[Issue, ...]:
    """校验 JSON 值并汇集所有结构问题。

    Args:
        data: 模型或调用方产生的 JSON 值。
        schema: 已经 normalize_schema 预检的 Schema。

    Returns:
        按 JSON Pointer 排序的问题元组；无问题时为空。
    """
    validator = validator_for(schema)(schema, format_checker=FormatChecker())
    return tuple(
        Issue(code="schema_validation", message=error.message, path=pointer(error.absolute_path))
        for error in sorted(
            validator.iter_errors(data), key=lambda item: pointer(item.absolute_path)
        )
    )
