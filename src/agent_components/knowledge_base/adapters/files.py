"""在本地文件系统中持久化知识库产物。"""

import os
import re
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from agent_components.knowledge_base.artifacts import ArtifactKind, ArtifactRef, _check_artifact

_ARTIFACT_FILENAME = re.compile(r"(source|parsed|markdown)-([0-9a-f]{64})")


class LocalArtifactStore:
    """在指定目录内按产物类别和内容摘要保存字节。"""

    def __init__(self, root: Path) -> None:
        """将现有或待创建的目录绑定为产物根目录。"""
        self._root = root.resolve()

    def _path(self, ref: ArtifactRef) -> Path:
        validated = ArtifactRef.model_validate(ref)
        return self._root / f"{validated.kind}-{validated.sha256}"

    def bind_workspace(self, identity: str) -> None:
        """原子绑定目录到唯一内容仓储，允许该仓储的多个进程打开。"""
        owner = sha256(identity.encode("utf-8")).hexdigest().encode("ascii")
        marker = self._root / ".workspace"
        self._root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            temp = NamedTemporaryFile(dir=self._root, delete=False)
            pending = Path(temp.name)
            try:
                with temp:
                    temp.write(owner)
                    temp.flush()
                    os.fsync(temp.fileno())
                try:
                    os.link(pending, marker)
                except FileExistsError:
                    # 并行进程可能已建立绑定，继续核验实际写入者。
                    pass
            finally:
                pending.unlink(missing_ok=True)
        if marker.read_bytes() != owner:
            raise ValueError("artifact directory already belongs to a different workspace")

    def put(self, kind: ArtifactKind, data: bytes) -> ArtifactRef:
        """通过同目录临时文件和原子替换发布完整内容。"""
        ref = ArtifactRef(kind=kind, sha256=sha256(data).hexdigest(), size=len(data))
        path = self._path(ref)
        self._root.mkdir(parents=True, exist_ok=True)
        temp = NamedTemporaryFile(dir=self._root, delete=False)
        pending = Path(temp.name)
        try:
            with temp:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
        return ref

    def read(self, ref: ArtifactRef) -> bytes:
        """读取产物并验证其字节数和 SHA-256 摘要。"""
        data = self._path(ref).read_bytes()
        _check_artifact(ref, data)
        return data

    def delete(self, ref: ArtifactRef) -> None:
        """删除产物。共享引用的生命周期由调用方管理。"""
        self._path(ref).unlink(missing_ok=True)

    def list_refs(self) -> tuple[ArtifactRef, ...]:
        """列出可用于回收检查的文件身份，忽略绑定标记和临时文件。"""
        if not self._root.exists():
            return ()
        refs = []
        for path in sorted(self._root.iterdir()):
            match = _ARTIFACT_FILENAME.fullmatch(path.name)
            if match is not None and path.is_file():
                kind, digest = match.groups()
                refs.append(
                    ArtifactRef(
                        kind=cast(ArtifactKind, kind), sha256=digest, size=path.stat().st_size
                    )
                )
        return tuple(refs)
