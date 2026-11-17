# 已开发组件的示例与接入文档

用户要求：为已开发组件提供示例和文档，并提交同一 GitHub 仓库。沿用既定的独立组件、中文说明、真实 API 和精确边界；不改变 SDK 公共行为。

采用独立 Python 模块＋按组件分组的 MD。每个例子显式接受文件／配置，输出 JSON 或 JSONL，说明依赖、凭据、输入输出、失败方式和资源生命周期。CLI 通过 `python -m examples.<name>` 运行。

考虑过把全部内容放在单一业务工作流或 Notebook 中；它们不利于独立调用和命令行验收，因此采用与既有组件边界一致的脚本方式。这是本轮文件组织的实现选择。

覆盖：Docling 标准解析与原生无模型 PDF、六种分块策略、章节／显式父子块、流式、父邻上下文、MMR／去重、LanceDB、SQLite、执行控制、模型规则路由／日志／结构化抽取、AgentScope Context／权限／Skills／原生 SQL、LangExtract Provider、确定性评测。

原生无模型 PDF 是框架集成示例，不冒充当前 DoclingParser 的新配置。LangExtract 只展示已经存在的 Provider 桥接。AgentScope 不示范尚未完成的整套业务编排或 HITL 恢复。

模型类例子使用真实 SDK，通过环境变量取得明确的模型和服务地址；默认验收不访问公网、不下载权重、不产生付费模型调用。测试只在模型传输边界使用本地 HTTP 服务，保留真实 AgentScope／OpenAI 解析路径。

验收：真实命令执行、输入文件保护、错误退出、模型路由与无正文日志、可选依赖隔离；全部既有测试、Ruff、mypy、锁文件和 sdist 检查。最终通过 GitHub 连接提交，并核对远端文件摘要。
