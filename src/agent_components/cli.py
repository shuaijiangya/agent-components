"""提供本地依赖版本诊断入口。

只读取包元数据，不导入可选框架或自动下载模型。
"""

import argparse
import importlib.metadata
import json
import sys


def main() -> None:
    """输出当前 Python 和组件依赖版本。

    doctor 是当前唯一子命令；缺少的可选依赖在 JSON 中显示为 null。
    """
    parser = argparse.ArgumentParser(prog="agent-components")
    parser.add_argument("command", choices=["doctor"])
    parser.parse_args()
    packages = [
        "enterprise-agent-components",
        "pydantic",
        "jsonschema",
        "tenacity",
        "docling",
        "chonkie",
        "sqlalchemy",
        "aiosqlite",
        "agentscope",
        "langextract",
        "lancedb",
        "deepeval",
        "ir-measures",
    ]
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    print(json.dumps({"python": sys.version.split()[0], "packages": versions}, indent=2))
