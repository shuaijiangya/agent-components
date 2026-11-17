"""演示 AgentScope 的独立适配组件。

权限检查不执行工具，Skill 枚举不挂载执行，上下文快照不自动持久化。
"""

import argparse
import asyncio
from pathlib import Path

from agent_components.core.contracts import RuntimeRef
from examples._common import emit, output_arg, protect, protect_sqlite, run


async def execute(args: argparse.Namespace) -> None:
    """延迟加载原生框架，只执行所选组件。"""
    if args.command == "skills":
        from agent_components.adapters.agentscope.skills import AgentScopeSkillAdapter

        rows = await AgentScopeSkillAdapter().list_skills(str(args.input))
        emit([row.model_dump(mode="json") for row in rows], args.output)
    elif args.command == "permission":
        from agentscope.permission import (
            PermissionBehavior,
            PermissionContext,
            PermissionEngine,
            PermissionRule,
        )
        from agentscope.tool import FunctionTool

        from agent_components.adapters.agentscope.runtime import AgentScopePermissionAdapter

        def lookup_contract(contract_id: str) -> dict[str, str]:
            """声明一个待检查权限的只读工具；本命令不会执行函数体。"""
            raise RuntimeError("permission example must not execute this tool")

        engine = PermissionEngine(PermissionContext())
        if args.deny:
            engine.add_rule(
                PermissionRule(
                    tool_name="lookup_contract",
                    rule_content=None,
                    behavior=PermissionBehavior.DENY,
                    source="projectSettings",
                )
            )
        tool = FunctionTool(lookup_contract, name="lookup_contract", is_read_only=True)
        emit(
            await AgentScopePermissionAdapter(engine).check(
                tool, {"contract_id": "contract-2026-001"}
            ),
            args.output,
        )
    elif args.command == "storage":
        from agent_components.adapters.agentscope.storage import create_native_sql_storage

        async with create_native_sql_storage(args.db, create_tables=args.create_tables) as storage:
            rows = await storage.list_agents(args.user_id)
            emit([row.model_dump(mode="json") for row in rows], args.output)
    else:
        from agentscope.agent import Agent

        from agent_components.adapters.agentscope.runtime import AgentScopeContextAdapter
        from examples._models import clients

        async with clients(args.config) as (_, native):
            agent = Agent(
                name="contract-assistant",
                system_prompt="根据给定合同回答问题。",
                model=native[args.model_alias],
            )
            if args.state:
                from agent_components.core.contracts import StateEnvelope
                from examples._common import read_json

                envelope = StateEnvelope.model_validate(read_json(args.state))
                if envelope.backend != "agentscope" or envelope.schema_version != "2.0.11":
                    raise ValueError("state backend/version requires an explicit migration")
                # 状态恢复走原生类型校验，不直接把任意 dict 赋给 Agent。
                agent.state = type(agent.state).model_validate(envelope.payload)
            ref = RuntimeRef(id=args.runtime_id, backend="agentscope")
            adapter = AgentScopeContextAdapter(ref, agent)
            snapshot = await (adapter.compact(ref) if args.compact else adapter.snapshot(ref))
            emit(snapshot, args.output)


def main() -> None:
    """暴露与业务流程无关的原生适配调用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    skills = commands.add_parser("skills")
    skills.add_argument("input", type=Path)
    permission = commands.add_parser("permission")
    permission.add_argument("--deny", action="store_true")
    storage = commands.add_parser("storage")
    storage.add_argument("--db", type=Path, required=True)
    storage.add_argument("--user-id", required=True)
    storage.add_argument("--create-tables", action="store_true")
    context = commands.add_parser("context")
    context.add_argument("--config", type=Path, required=True)
    context.add_argument("--model-alias", required=True)
    context.add_argument("--runtime-id", default="contract-session-1")
    context.add_argument("--state", type=Path)
    context.add_argument("--compact", action="store_true", help="由原生配置决定是否请求模型")
    for cmd in (skills, permission, storage, context):
        output_arg(cmd)
    args = parser.parse_args()
    if args.command == "storage":
        protect_sqlite(args.db, args.output)
    if args.command == "skills" and args.output:
        # 枚举结果不得写回 Skill 包，避免覆盖资源或改变下次计算的版本。
        if args.output.resolve().is_relative_to(args.input.resolve()):
            raise ValueError("skill inventory output must be outside the skill directory")
    protect(args.output, *(getattr(args, name, None) for name in ("config", "state", "db")))
    asyncio.run(execute(args))


if __name__ == "__main__":
    run(main)
