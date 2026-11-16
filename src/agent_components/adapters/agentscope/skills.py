"""使用原生 SkillLoader 发现 Skill 并计算内容版本。

文件系统读取在工作线程执行；该模块不实现 Skill 执行引擎。
"""

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from agent_components.core.contracts import SkillDescriptor
from agent_components.core.errors import MissingDependencyError


class AgentScopeSkillAdapter:
    """发现本地 Skill，检查包边界和资源大小。

    原生加载器可能跳过无效 Skill；发现列表不代表全部来源文件均有效。
    """

    def __init__(self, *, max_package_bytes: int = 10_000_000) -> None:
        """设置单个 Skill 包的读取上限。

        Args:
            max_package_bytes: 计算内容版本时允许的单包总字节数。
        """
        self._limit = max_package_bytes

    async def list_skills(self, source: str) -> tuple[SkillDescriptor, ...]:
        """调用原生加载器并补充资源内容摘要。

        Args:
            source: 本地 Skill 根目录。

        Returns:
            包含全部资源内容版本的 Skill 描述元组。

        Raises:
            ValueError: 来源非目录、名称重复、资源越界或包过大。
            MissingDependencyError: 未安装 agent extra。
        """
        try:
            from agentscope.skill import LocalSkillLoader
        except ImportError as exc:
            raise MissingDependencyError("agent") from exc
        root = await asyncio.to_thread(_resolve_root, source)
        skills = await LocalSkillLoader(str(root), scan_subdir=True).list_skills()
        result = []
        names: set[str] = set()
        for skill in skills:
            if skill.name in names:
                raise ValueError(f"Duplicate skill name: {skill.name}")
            names.add(skill.name)
            result.append(await asyncio.to_thread(_describe, skill, root, self._limit))
        return tuple(result)


def _resolve_root(source: str) -> Path:
    root = Path(source).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Skill source must be a directory")
    return root


def _describe(skill: Any, root: Path, limit: int) -> SkillDescriptor:
    """验证资源边界并计算 Skill 包版本。

    Args:
        skill: 原生加载器返回的 Skill。
        root: 已解析的来源根目录。
        limit: 单包最大总字节数。

    Returns:
        按相对路径和文件内容计算版本的 Skill 描述。
    """
    skill_root = Path(skill.dir).resolve()
    if not skill_root.is_relative_to(root):
        raise ValueError("Skill root escapes the configured source")
    digest = hashlib.sha256()
    total = 0
    for path in sorted(skill_root.rglob("*")):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(skill_root):
            raise ValueError("Skill resource escapes its package")
        total += path.stat().st_size
        if total > limit:
            raise ValueError("Skill package exceeds configured byte limit")
        digest.update(path.relative_to(skill_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return SkillDescriptor(
        id=skill.name, name=skill.name, description=skill.description, version=digest.hexdigest()
    )
