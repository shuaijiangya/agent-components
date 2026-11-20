"""提供可替换的真实 Tokenizer 与同步 Embedding 工厂。

Tokenizer 只读本地文件，Embedding 只请求显式配置的兼容端点。
"""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_components.models.providers.openai_compatible import OpenAICompatibleEmbeddings


def local_tokenizer() -> Any:
    """从 TOKENIZER_PATH 读取匹配目标模型的 tokenizer.json，不下载资源。"""
    from tokenizers import Tokenizer

    native = Tokenizer.from_file(str(Path(os.environ["TOKENIZER_PATH"])))

    class Encoder:
        """按真实 Token ID 计量；特殊 Token 的策略应与使用方模型保持一致。

        此示例关闭自动添加特殊 Token，调用方需为模型提示另外预留空间。
        """

        def encode(self, text: str) -> Sequence[int]:
            """返回正文的真实 Token ID，不使用字符数近似。"""
            return native.encode(text, add_special_tokens=False).ids

    return Encoder()


class CompatibleEmbeddings(OpenAICompatibleEmbeddings):
    """根据示例环境变量创建模型包的 Embedding 客户端。"""

    def __init__(self) -> None:
        """读取示例约定的环境变量并初始化端点。"""
        super().__init__(
            dimension=int(os.environ["EMBEDDING_DIMENSION"]),
            model=os.environ["EMBEDDING_MODEL"],
            api_key=os.environ["EMBEDDING_API_KEY"],
            base_url=os.environ["EMBEDDING_BASE_URL"],
        )


def compatible_embeddings() -> CompatibleEmbeddings:
    """创建可由公共示例辅助层自动关闭的同步 Provider。"""
    return CompatibleEmbeddings()
