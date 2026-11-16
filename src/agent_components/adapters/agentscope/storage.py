"""创建原生 SQL Storage，不启动 Agent Service。

原生表与应用状态表独立管理，安装依赖不等于部署服务。
"""

from pathlib import Path
from typing import Any

from agent_components.core.errors import MissingDependencyError


def create_native_sql_storage(path: Path, *, create_tables: bool = False) -> Any:
    """创建使用本地 SQLite 的原生存储实例。

    Args:
        path: SQLite 文件路径，必要时创建父目录。
        create_tables: 是否由原生存储初始化表，默认关闭。

    Returns:
        AgentScope AsyncSQLAlchemyStorage 对象，由调用方管理生命周期。

    Raises:
        MissingDependencyError: 未安装兼容的 agent-storage extra。
    """
    try:
        from agentscope.app.storage import AsyncSQLAlchemyStorage
    except ImportError as exc:
        # 2.0.11 eagerly imports agentscope.app before app.storage.
        raise MissingDependencyError("agent-storage") from exc
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{path}", create_tables=create_tables)
