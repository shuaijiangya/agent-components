# Component Examples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为现有组件提供独立、可运行、带中文说明的示例并提交 GitHub。

**Architecture:** 示例只消费现有 SDK 与原生框架接口；公共 CLI 文件处理和模型配置放在 examples 的私有辅助模块。无需凭据的能力真实运行；模型服务在明确配置后才调用。

**Tech Stack:** Python 3.12.14、现有 pyproject 分组、argparse、pytest、Docling、Chonkie、AgentScope。

**Spec:** docs/superpowers/specs/2026-11-17-component-examples-design.md

## Global Constraints

- 不修改 src 的公共行为，不新增依赖、不隐式下载模型。
- 中文多行注释；不把外部模型准确率、PDF 标准推理或未实施能力写成已验收。
- 源文档不能作为输出文件覆盖；持久目录由使用方指定。
- 模型配置只存环境变量名称，供应商异常不打印正文或凭据。

## Review Focus

- 未安装可选框架时，core 示例和所有 --help 可用。
- 重复执行持久化示例时，版本冲突和幂等写入行为正确。
- 输出路径与输入相同时，不能先截断源文件。
- 模型别名覆盖和任务路由真实传到传输层，关闭重叠重试。
- 无模型 PDF 不下载权重，且不能宣称保留标题／表格结构。

## Task 1: 文件与分块示例

- [x] 写入 CLI 行为测试，先观察因例子不存在而失败。
- [x] 新增 examples 的文件读写辅助、解析、六策略、父子块、流式、上下文和选择命令及输入文件。
- [x] 执行对应测试，确认原文、来源、预算、错误和源文件保护。

## Task 2: 模型、存储与框架示例

- [x] 写入真实本地 HTTP、SQLite、LanceDB、AgentScope、LangExtract 和评测命令测试。
- [x] 实现环境变量配置、模型客户端生命周期及独立示例。
- [x] 验证实际接口与错误，模型请求只送往测试启动的本地服务。

## Task 3: 文档、分发和提交

- [x] 新增 docs/examples 索引和逐组件文档，连接示例源码与命令。
- [x] 更新 README、sdist、CI 中的示例检查范围。
- [x] 全量 pytest、Ruff、mypy、核心环境、构建和独立审查。
- [x] 整理全部待提交文件、构建验证和交付校验方式。

交付验收：提交到 GitHub main 后，比较远端 tree 与本地暂存树，并核对 main 提交身份；以 GitHub 的实际提交记录为准。
