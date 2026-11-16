"""声明可替换后端必须实现的最小能力协议。

Adapter 按实际支持的能力实现协议，不强制共享具体框架对象。
"""

from collections.abc import Sequence
from typing import Protocol

from agent_components.core.contracts import (
    Chunk,
    Document,
    ExtractionOptions,
    ExtractionResult,
    ModelOutput,
    ModelRequest,
    RuntimeRef,
    SkillDescriptor,
    StateEnvelope,
)
from agent_components.core.schema import Schema


class StructuredModel(Protocol):
    """约定结构化模型调用能力。

    具体供应商对象应留在 Adapter 内，公共入口仅交换契约对象。
    """

    async def generate(self, request: ModelRequest) -> ModelOutput:
        """生成目标结构的数据。

        Args:
            request: 统一模型请求。

        Returns:
            数据和用量，由上层执行 Schema 校验。
        """
        ...


class EvidenceBackend(Protocol):
    """约定文档字段与证据抽取能力。

    实现需显式表达部分失败、证据定位精度和覆盖限制。
    """

    async def extract(
        self,
        content: Document | Chunk | Sequence[Chunk],
        schema: Schema,
        options: ExtractionOptions,
    ) -> ExtractionResult:
        """提取业务字段及关联证据。

        Args:
            content: 文档或一个及多个块。
            schema: 目标业务结构。
            options: 抽取策略及路由选项。

        Returns:
            包含字段、证据和问题的抽取结果。
        """
        ...


class ContextBackend(Protocol):
    """约定原生上下文的读取和压缩能力。

    状态封套需要后端和版本一致才能恢复，不承诺跨框架直接加载。
    """

    async def snapshot(self, runtime: RuntimeRef) -> StateEnvelope:
        """获取原生状态快照。

        Args:
            runtime: Adapter 绑定的运行标识。

        Returns:
            版本化的后端状态封套。
        """
        ...

    async def compact(self, runtime: RuntimeRef) -> StateEnvelope:
        """请求后端压缩上下文并返回结果。

        Args:
            runtime: Adapter 绑定的运行标识。

        Returns:
            压缩后的原生状态封套。
        """
        ...


class SkillBackend(Protocol):
    """约定 Skill 发现能力。

    协议不包含运行、挂载或动态安装 Skill。
    """

    async def list_skills(self, source: str) -> Sequence[SkillDescriptor]:
        """发现给定来源中的 Skill。

        Args:
            source: 后端支持的来源路径。

        Returns:
            带有内容版本的 Skill 描述序列。
        """
        ...
