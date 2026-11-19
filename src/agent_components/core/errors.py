"""定义组件调用方可捕获的稳定异常类型。

code 提供跨 Adapter 的错误分类，底层异常可通过异常链定位。
"""


class ComponentError(Exception):
    """作为所有组件分类异常的基类。

    code 是程序可识别的分类，不包含业务重试或审批决策。
    """

    code = "component_error"


class ChunkAlignmentError(ComponentError):
    """分块后端违反位置、内容、大小或完整覆盖约束。

    调用方应停止发布本批分块；组件不会用模糊查找猜测原文位置。
    """

    code = "chunk_alignment_error"


class MissingDependencyError(ComponentError):
    """表示调用的可选能力尚未安装。

    错误消息给出需安装的 extras，不在运行中自动安装软件包。
    """

    code = "missing_dependency"

    def __init__(self, extra: str) -> None:
        """生成明确的可选依赖安装提示。

        Args:
            extra: pyproject.toml 中的可选依赖组名称。
        """
        super().__init__(f"Install enterprise-agent-components[{extra}] to use this backend")


class UnsupportedCapabilityError(ComponentError):
    """表示当前 Adapter 或配置不支持所请求的能力。

    调用方可以选择其他后端，不应把缺少能力当作成功。
    """

    code = "unsupported_capability"


class InvalidSchemaError(ComponentError):
    """表示目标 JSON Schema 本身无效。

    应修正输入契约后再调用模型，而非重试相同请求。
    """

    code = "invalid_schema"


class RoutingError(ComponentError):
    """表示模型别名或路由配置无效。

    在请求发往供应商之前抛出。
    """

    code = "routing_error"


class RevisionConflictError(ComponentError):
    """表示持久化内容或状态的版本条件不再满足。

    调用方应重新读取状态并决定如何处理冲突，不自动覆盖。
    """

    code = "revision_conflict"


class RetryableError(ComponentError):
    """显式标记允许有限重试的瞬时失败。

    业务写操作是否可重试由调用方负责分类，执行器不会自行推断幂等性。
    """

    code = "retryable_error"
