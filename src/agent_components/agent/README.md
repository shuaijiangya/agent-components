# Agent 运行与编排

`AgentBackend` 定义统一的 `run` 和 `stream` 接口。首期的 `AgentScopeAdapter` 使用 AgentScope 原生推理、权限检查和工具循环。本包提供公开请求、消息、工具、事件及结果类型。安装与仓库锁定的 AgentScope 一致的 `agent` 依赖组即可运行。

## 业务接入

先配置 OpenAI 兼容端点的模型名、基础 URL 和 API Key 环境变量。以下代码由应用注入已配置的原生模型，配置文件可使用仓库示例。[模型配置说明](../models/README.md#配置端点并调用)

```python
from pydantic import BaseModel, Field, JsonValue

from agent_components.adapters.agentscope import AgentScopeAdapter
from agent_components.agent import AgentConfig, AgentRequest, BaseTool, ToolContext, ToolRegistry


class LookupOrder(BaseModel):
    number: int = Field(gt=0)


registry = ToolRegistry()


class LookupOrderTool(BaseTool[LookupOrder], registry=registry):
    name = "lookup_order"
    description = "按订单号查询订单状态。"
    parameters = LookupOrder
    read_only = True

    async def execute(self, params: LookupOrder, context: ToolContext) -> JsonValue:
        fixture = {"demo-tenant": {42: "ready"}}
        return {"status": fixture.get(context.tenant_id or "", {}).get(params.number, "not_found")}


async def query_order(native_model):
    agent = AgentScopeAdapter(
        model=native_model,
        config=AgentConfig(
            name="orders",
            system_prompt="先查询系统中的真实状态，再回复用户。",
            timeout_seconds=30,
            max_iterations=4,
            max_concurrency=8,
        ),
        tools=registry.list_tools(),
    )
    return await agent.run(
        AgentRequest.from_text("查询订单42现在能否交付", request_id="order-request-1"),
        context=ToolContext(tenant_id="demo-tenant", user_id="operator"),
    )
```

`BaseTool` 在导入工具定义模块时自动注册具体子类。子类提供 `name`、`description`、`parameters` 和异步 `execute()`。省略 `registry=` 时，根工具类使用公开的 `DEFAULT_TOOL_REGISTRY`，子类继承父类所绑定的注册表。可以用抽象基类为一组工具绑定同一个注册表。

工具类需要支持无参构造，每次执行创建一个新实例。注册发生在类定义时，工具实例的构造和执行发生在调用时。重名沿用 `ToolRegistry` 的检查，发现冲突时会显式报错。创建 Agent 前先导入工具定义模块，再取得 `list_tools()` 快照。

有多条历史消息时，使用 `AgentRequest(messages=(...))` 传入应用已确定的用户、助手和系统消息。每次运行创建独立的原生 Agent 状态和 Toolkit。模型客户端可在多个运行间复用，由注入方负责关闭。

```bash
uv sync --locked --extra agent
uv run --no-sync python -m examples.agent_runtime --config examples/data/models.json
uv run --no-sync python -m examples.agent_runtime --config examples/data/models.json --stream
```

`--model-alias` 选择已配置的原生模型，缺省使用配置中的默认别名。CLI 使用虚构的租户和订单数据。示例的模型配置来自已有工厂，原生工具循环使用当次注入的模型。Agent 运行不提供整个循环的自动重试。

## 统一注册多个工具来源

函数工具可通过 `ToolRegistry.register()` 注册单个 `AgentTool`，也可用 `register_many()` 批量注册。MCP 或 Skill 适配器实现 `ToolSource.list_tools()`，输出同一个 `AgentTool` 契约。通过 `register_source()` 注册即可接入 Agent。

```python
from collections.abc import Sequence
from typing import Any

from agent_components.agent import AgentTool, ToolRegistry, ToolSource


async def assemble_tools(
    local: Sequence[AgentTool[Any]], mcp_source: ToolSource, skill_source: ToolSource
) -> list[AgentTool[Any]]:
    registry = ToolRegistry()
    registry.register_many(local)
    await registry.register_source(mcp_source, prefix="mcp")
    await registry.register_source(skill_source, prefix="skill")
    return registry.list_tools()
```

`AgentTool.parameters` 接受 Pydantic 模型类型或 JSON Schema 字典。异步处理器接收校验后的模型实例或 JSON 对象，以及由应用注入的 `ToolContext`。JSON Schema 支持本地片段引用，外部引用需要由来源适配器预先展开。显式设定 `read_only=True` 才可按只读工具执行。来源的连接、发现逻辑和调用实现由各适配器提供。

注册表按注册顺序输出独立的 `list[AgentTool]` 快照。出现重名或配置无效时，本批次不会写入。来源可加前缀区分同名工具。前缀改变模型可见的名称，处理器仍可使用来源中的原始标识。

## 流式调用与结果

```python
from contextlib import aclosing

async with aclosing(agent.stream(request, context=tool_context)) as events:
    async for event in events:
        if event.type == "message.text.delta":
            print(event.delta or "", end="", flush=True)
        elif event.type == "run.finished":
            print(event.result.model_dump_json(exclude_none=True))
```

事件带请求 ID、运行 ID、单调递增的序号和适用的回复 ID，覆盖模型调用、可见文本与媒体、工具参数、工具输出和终态。原生流式模型可逐段输出，原生非流式模型也会生成相同事件协议。中间轮次可能出现可见文本，最终交付以 `result.message` 或 `result.text` 为准。工具结果通过 `call.id` 与调用关联，失败和拒绝状态可以单独读取。

| 行为 | 使用约定 |
| --- | --- |
| 成功或业务工具失败 | `completed` 表示原生回复完成。检查 `tool_results` 获取每次工具调用的独立结果。 |
| 期限与并发 | 总期限包含排队和原生运行。超时返回 `timed_out`，取消消费任务会传播 `CancelledError`。提前停止流时使用 `aclosing` 释放并发名额。 |
| 原生循环 | `max_iterations` 映射到 AgentScope 的 ReAct 上限。原生框架在达到上限时可用一次收尾推理，并以 `iteration_limit` 报告。 |
| 工具参数 | 检查原始 JSON 的键、类型和业务约束。校验失败时工具处理器不会执行。可信租户、用户信息由应用通过 `ToolContext` 注入。 |
| 权限与恢复 | 只有明确标注为只读的业务工具可直接执行。其他工具遵循原生权限，返回 `needs_confirmation` 和待确认调用。持久审批、审批决定提交与恢复属于后续 HITL 组件。 |
| 异常 | `failed` 附带稳定错误码、原生异常类型和可用的 HTTP 状态。SDK 输出省略供应商及工具异常原文。 |

`AgentScopeAdapter` 适合请求级运行与应用显式传入历史的用法。会话恢复、持久化执行审计及跨组件费用预算由对应组件封装。详见 [技术方案第 7 节](../../../agent-components-technical-summary.md#7-agent-相关第-19-点)。
