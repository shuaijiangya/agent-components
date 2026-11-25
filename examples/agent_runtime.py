"""用配置的 AgentScope 模型运行订单查询示例。"""

import argparse
import asyncio
from contextlib import aclosing
from pathlib import Path

from pydantic import BaseModel, Field, JsonValue

from agent_components.adapters.agentscope import AgentScopeAdapter
from agent_components.agent import AgentConfig, AgentRequest, BaseTool, ToolContext, ToolRegistry
from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)
from examples._common import emit, run


class LookupOrder(BaseModel):
    """订单查询工具的业务参数。"""

    number: int = Field(gt=0)


ORDER_TOOLS = ToolRegistry()


class LookupOrderTool(BaseTool[LookupOrder], registry=ORDER_TOOLS):
    """从业务服务指定的租户读取示例订单。"""

    name = "lookup_order"
    description = "按正整数订单号查询订单状态。"
    parameters = LookupOrder
    read_only = True

    async def execute(self, params: LookupOrder, context: ToolContext) -> JsonValue:
        """返回调用方租户的订单状态。"""
        orders = {"demo-tenant": {42: "ready"}}
        return {
            "number": params.number,
            "status": orders.get(context.tenant_id or "", {}).get(params.number, "not_found"),
        }


async def execute(args: argparse.Namespace) -> int:
    """运行一次查询，输出最终 JSON 或带终态的 JSONL。"""
    settings = OpenAIModelSettings.model_validate_json(args.config.read_bytes())
    async with openai_compatible_clients(settings) as (_, native):
        alias = args.model_alias or settings.default_alias
        adapter = AgentScopeAdapter(
            model=native[alias],
            config=AgentConfig(
                name="orders", system_prompt="使用查询工具核实订单状态，再用中文回复。"
            ),
            tools=ORDER_TOOLS.list_tools(),
        )
        request = AgentRequest.from_text(args.prompt)
        context = ToolContext(tenant_id="demo-tenant")
        if args.stream:
            status = None
            async with aclosing(adapter.stream(request, context=context)) as events:
                async for event in events:
                    emit(event.model_dump(mode="json", exclude_none=True))
                    if event.result is not None:
                        status = event.result.status
            return int(status != "completed")
        result = await adapter.run(request, context=context)
        emit(result)
        return int(result.status != "completed")


def main() -> int:
    """从环境变量关联的配置选择模型，展示最小可运行集成。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-alias")
    parser.add_argument("--prompt", default="查询订单42现在能否交付")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    return asyncio.run(execute(args))


if __name__ == "__main__":
    run(main)
