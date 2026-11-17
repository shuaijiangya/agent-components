"""提供可替换的真实 Tokenizer 与同步 Embedding 工厂。

Tokenizer 只读本地文件，Embedding 只请求显式配置的兼容端点。
"""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any


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


class CompatibleEmbeddings:
    """封装同步 OpenAI 兼容向量端点，满足分块组件的同步协议。

    dimension 必须与部署模型实际输出一致；不隐式归一化或截断向量。
    """

    def __init__(self) -> None:
        """在工厂调用时读取环境变量，关闭 HTTP SDK 隐式重试。"""
        from openai import OpenAI

        self.dimension = int(os.environ["EMBEDDING_DIMENSION"])
        self.model = os.environ["EMBEDDING_MODEL"]
        self.client = OpenAI(
            api_key=os.environ["EMBEDDING_API_KEY"],
            base_url=os.environ["EMBEDDING_BASE_URL"],
            timeout=30,
            max_retries=0,
        )

    def embed_batch(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """校验响应序号后恢复输入顺序。

        Args:
            texts: 分块组件提交的一批规范文本。

        Returns:
            与输入位置一一对应的向量；具体维度和数值由 SDK 分块器验证。

        Raises:
            ValueError: 供应商返回缺失、重复或越界序号。
        """
        response = self.client.embeddings.create(model=self.model, input=list(texts))
        if sorted(row.index for row in response.data) != list(range(len(texts))):
            raise ValueError("embedding response indices do not match inputs")
        return [row.embedding for row in sorted(response.data, key=lambda row: row.index)]

    def close(self) -> None:
        """释放同步 HTTP 连接池。"""
        self.client.close()


def compatible_embeddings() -> CompatibleEmbeddings:
    """创建可由公共示例辅助层自动关闭的同步 Provider。"""
    return CompatibleEmbeddings()
