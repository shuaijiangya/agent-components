"""通过事务和版本条件保存公共状态封套。

应用表使用独立迁移，不与 AgentScope 原生表共用恢复语义。
"""

from pathlib import Path
from typing import Any, Self

from agent_components.core.contracts import StateEnvelope, StoredState
from agent_components.core.errors import MissingDependencyError, RevisionConflictError

try:
    from sqlalchemy import (
        JSON,
        Column,
        Integer,
        MetaData,
        String,
        Table,
        event,
        insert,
        select,
        update,
    )
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine
except ImportError as exc:
    raise MissingDependencyError("storage") from exc

metadata = MetaData()
states = Table(
    "component_states",
    metadata,
    Column("namespace", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("state", JSON, nullable=False),
)


class SQLiteStateStore:
    """在命名空间内提供乐观并发控制的状态存储。

    应通过 async with 使用；生产环境应先运行迁移，避免隐式创建表。
    """

    def __init__(self, path: Path, *, create_tables: bool = False) -> None:
        """配置本地 SQLite 引擎和建表选项。

        Args:
            path: 数据文件路径，必要时创建父目录。
            create_tables: 开发时可显式建表；默认使用外部迁移。
        """
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self._create_tables = create_tables

        @event.listens_for(self._engine.sync_engine, "connect")
        def configure(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def __aenter__(self) -> Self:
        """进入存储上下文并按显式选项初始化表。

        Returns:
            当前存储实例。
        """
        if self._create_tables:
            async with self._engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
        return self

    async def __aexit__(self, *_: Any) -> None:
        """释放数据库连接池。

        Args:
            _: 上下文退出传入的异常信息，不抑制异常。
        """
        await self._engine.dispose()

    async def load(self, namespace: str, key: str) -> StoredState | None:
        """读取当前状态及其存储版本。

        Args:
            namespace: 调用方管理的隔离命名空间。
            key: 命名空间内的状态键。

        Returns:
            已存在的状态；缺失时为 None。
        """
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(states).where(
                            states.c.namespace == namespace,
                            states.c.key == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return StoredState(
            namespace=namespace,
            key=key,
            revision=row["revision"],
            state=StateEnvelope.model_validate(row["state"]),
        )

    async def save(
        self, namespace: str, key: str, state: StateEnvelope, *, expected_revision: int
    ) -> StoredState:
        """以事务比较并更新状态版本。

        Args:
            namespace: 状态所属命名空间。
            key: 状态键。
            state: 待保存的版本化后端状态。
            expected_revision: 零表示新建，否则必须等于当前存储版本。

        Returns:
            写入后的状态，新 revision 为 expected_revision 加一。

        Raises:
            ValueError: expected_revision 为负数。
            RevisionConflictError: 已存在同名状态或当前版本与预期不符。
        """
        if expected_revision < 0:
            raise ValueError("expected_revision must be nonnegative; 0 means create")
        revision = expected_revision + 1
        try:
            async with self._engine.begin() as connection:
                if expected_revision == 0:
                    await connection.execute(
                        insert(states).values(
                            namespace=namespace,
                            key=key,
                            revision=revision,
                            state=state.model_dump(mode="json"),
                        )
                    )
                else:
                    result = await connection.execute(
                        update(states)
                        .where(
                            states.c.namespace == namespace,
                            states.c.key == key,
                            states.c.revision == expected_revision,
                        )
                        .values(revision=revision, state=state.model_dump(mode="json"))
                    )
                    if result.rowcount != 1:
                        raise RevisionConflictError("state revision no longer matches")
        except IntegrityError as exc:
            raise RevisionConflictError("state already exists") from exc
        return StoredState(namespace=namespace, key=key, revision=revision, state=state)
