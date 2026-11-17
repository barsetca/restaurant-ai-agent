from typing import Protocol

from langchain_openai import OpenAIEmbeddings


class EmbeddingsProvider(Protocol):
    model_name: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class LangChainOpenAIEmbeddings:
    def __init__(self, api_key: str, model_name: str, dimensions: int, timeout_seconds: float) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self._client = OpenAIEmbeddings(
            api_key=api_key,
            model=model_name,
            dimensions=dimensions,
            timeout=timeout_seconds,
            max_retries=2,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._client.aembed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self._client.aembed_query(text)
