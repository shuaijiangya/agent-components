"""定义知识库文件产物的内容寻址引用和存储接口。"""

from collections.abc import Sequence
from hashlib import sha256
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from agent_components.core.contracts import Contract

type ArtifactKind = Literal["source", "parsed", "markdown"]
type _SHA256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ArtifactRef(Contract):
    """通过类别、SHA-256 摘要与字节数标识不可变产物。"""

    model_config = ConfigDict(revalidate_instances="always")

    kind: ArtifactKind
    sha256: _SHA256
    size: int = Field(ge=0, strict=True)


class ArtifactStore(Protocol):
    """根据内容写入和读取持久化产物，每个实例绑定独立仓储的产物空间。"""

    def put(self, kind: ArtifactKind, data: bytes) -> ArtifactRef:
        """持久化产物并返回其内容寻址引用。"""
        ...

    def read(self, ref: ArtifactRef) -> bytes:
        """读取并校验产物字节。"""
        ...

    def delete(self, ref: ArtifactRef) -> None:
        """删除指定产物，产物已不存在时直接返回。"""
        ...


@runtime_checkable
class WorkspaceArtifactStore(ArtifactStore, Protocol):
    """扩展文件端口，支持核对产物空间所属的内容仓储。"""

    def bind_workspace(self, identity: str) -> None:
        """首次绑定或校验现有绑定，防止独立仓储互相清除文件。"""
        ...


@runtime_checkable
class EnumerableArtifactStore(ArtifactStore, Protocol):
    """扩展文件端口，支持扫描未被内容仓储引用的产物。"""

    def list_refs(self) -> Sequence[ArtifactRef]:
        """列出当前产物空间中已提交的文件，忽略尚在写入的临时文件。"""
        ...


def _check_artifact(ref: ArtifactRef, data: bytes) -> None:
    if len(data) != ref.size or sha256(data).hexdigest() != ref.sha256:
        raise ValueError("artifact content does not match its size or SHA-256 reference")
