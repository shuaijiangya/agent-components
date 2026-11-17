# AgentScope 适配组件

源码：[runtime.py](../../examples/runtime.py)。绑定 AgentScope **2.0.11**，由公共契约隔离部分框架差异；不实现第二套 Agent 循环。

## 上下文快照与压缩

配置方法见 [模型接入](models.md)。创建原生 Agent 并输出当前快照：

```sh
uv run --no-sync python -m examples.runtime context --config examples/data/models.json --model-alias general --runtime-id contract-session-1 --output data/context-state.json
```

首次运行是一个空会话，用于检查原生初始化与序列化；不会伪造多轮历史或演示不存在的业务对话。已有同版本快照可恢复并重新输出：

```sh
uv run --no-sync python -m examples.runtime context --config examples/data/models.json --model-alias general --runtime-id contract-session-1 --state data/context-state.json --output data/context-state-restored.json
```

`--state` 接收 `StateEnvelope`，必须 `backend=agentscope`、`schema_version=2.0.11`，并通过原生状态类型校验。若状态来自公共 SQLite 存储，应传入 `StoredState.state` 部分。

添加 `--compact` 才调用原生 `compress_context()`，是否请求模型、如何摘要由原生配置及当前上下文决定；空会话一般无须模型。本轮只验收快照与同版本状态往返，未对长会话压缩质量和 Token 节省率作承诺。压缩完成不会自动写数据库。

真实应用可以将已运行的 Agent 绑定适配器：

```python
from agent_components.adapters.agentscope.runtime import AgentScopeContextAdapter
from agent_components.core.contracts import RuntimeRef

ref = RuntimeRef(id=session_id, backend="agentscope")
adapter = AgentScopeContextAdapter(ref, running_agent)
state = await adapter.snapshot(ref)
```

快照可能包含业务上下文，需由使用方控制访问与存储。状态恢复属于版本约束明确的原生集成，不能跨框架直接恢复。

## 工具权限判定

```sh
uv run --no-sync python -m examples.runtime permission
uv run --no-sync python -m examples.runtime permission --deny
```

示例声明只读的 `lookup_contract`，分别验证默认只读允许与显式 deny 优先。工具函数体刻意禁止执行，权限判定不会读取业务合同。

`AgentScopePermissionAdapter.check(tool, parameters)` 把原生结果转成 `allow/deny/pending`。`pending` 表示需要确认，**不包含审批页面、持久化、超时或恢复**；使用方应在业务执行边界处理权限结果。

## Skill 枚举与资源版本

```sh
uv run --no-sync python -m examples.runtime skills examples/data/skills --output data/skills.json
```

输入是本地 Skill 根目录，每个包具有 `SKILL.md` frontmatter。返回 Skill 描述和基于全部资源内容计算的版本；资源变化会改变版本，重名 Skill 会失败。这里不挂载 Skill，不执行指令，不授予工具权限。

## 原生 SQL Storage

```sh
uv run --no-sync python -m examples.runtime storage --db data/agentscope-native.db --user-id tenant-a --create-tables --output data/native-agents.json
```

使用 `agent-storage` extra，创建原生存储后调用真实 `list_agents(user_id)`；全新库返回 `[]`。该示例验证工厂、建表和资源释放，不伪造 Agent 运行历史。生产应按原生框架方式管理表结构，关闭显式开发建表选项。

安装该组会引入原生 service 相关依赖，这是 AgentScope 2.0.11 的导入链要求；创建存储不会启动 Agent Service。原生表与公共 `SQLiteStateStore` 表、状态封套和迁移语义独立，不能互相替代。

## 后续切换框架

当前可复用的是公共数据契约与端口，例如模型、状态、上下文、权限与 Skill 元信息。切换框架需要新 Adapter，并验证能力差异。业务 Agent 循环、工具原生对象和历史状态不会因为有 Adapter 就自动兼容；跨后端恢复需显式迁移。
