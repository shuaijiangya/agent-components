# 状态保存与执行控制

源码：[state.py](../../examples/state.py)、[execution.py](../../examples/execution.py)。两者是独立组件，没有隐含的 Agent 流程或业务事务。

## 公共状态封套

输入 `StateEnvelope`：`backend/schema_version/payload`；存储返回 `namespace/key/revision/state`。应用 `schema_version` 与数据库 CAS `revision` 是两种独立版本。

显式开发建表并创建状态：

```sh
uv run --no-sync python -m examples.state save --db data/state.db --namespace tenant-a --key proposal-001 --state examples/data/state.json --expected-revision 0 --create-tables
uv run --no-sync python -m examples.state load --db data/state.db --namespace tenant-a --key proposal-001 --output data/state-loaded.json
```

`expected_revision=0` 仅用于新建。已存在时再次执行同一创建命令会报 `RevisionConflictError`；这用于防止覆盖。读到 revision 1 后，可以明确更新：

```sh
uv run --no-sync python -m examples.state save --db data/state.db --namespace tenant-a --key proposal-001 --state examples/data/state.json --expected-revision 1
```

使用方应根据重新读到的状态决定冲突如何处理；不要盲目读最新 revision 后覆盖。`load` 不存在返回 JSON `null`，数据库连接由 `async with` 释放。

生产应先运行仓库 Alembic 迁移，禁用 `--create-tables`。迁移配置见 [数据库迁移](../DEPENDENCIES.md) 及 [alembic.ini](../../alembic.ini)。`namespace` 是调用方组织数据的命名空间，不自带身份认证或租户授权。

```sh
mkdir -p data
AGENT_COMPONENTS_DATABASE_URL=sqlite+aiosqlite:///./data/state.db uv run --no-sync alembic upgrade head
```

直接调用：

```python
from agent_components.storage.sqlite import SQLiteStateStore

async with SQLiteStateStore(database_path) as store:
    saved = await store.save(namespace, key, state, expected_revision=expected_revision)
```

这是保存状态原语；不能据此认为工具调用、数据库写入和状态保存是一个原子事务。审批队列、幂等键和外部操作补偿尚需使用方处理。

## 并发、总超时与显式重试

就绪探测示例建立并关闭真实 TCP 连接，用于验证部署端点可达性：

```sh
uv run --no-sync python -m examples.execution --host 127.0.0.1 --port 8000 --probes 8 --concurrency 4 --timeout 5 --max-attempts 2
```

端口未开启会明确失败。此示例不使用固定 sleep 伪造耗时；只有连接建立阶段的 `OSError` 被示例转换为 `RetryableError`。实际应用应进一步按网络故障分类。

```python
from agent_components.execution import ExecutionController, ExecutionPolicy

controller = ExecutionController(ExecutionPolicy(
    timeout_seconds=30, max_concurrency=4, max_attempts=2,
))
result = await controller.run(operation_factory)
```

`operation_factory` 必须是每次可重新创建异步调用的工厂。总期限包含排队、执行及退避；默认只尝试一次，只重试显式 `RetryableError`，取消信号向上传播。并发限制属于当前控制器实例，跨进程限流需要外部协调。

模型网关的 429／5xx、网络 SDK 异常不会自动变成 `RetryableError`。模型示例保留原始供应商异常分类；若要重试，需要明确映射并防止与原生框架策略叠加。业务写操作不能直接照搬 TCP 探测的重试策略。
