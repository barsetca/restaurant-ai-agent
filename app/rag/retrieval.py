from app.rag.embeddings import EmbeddingsProvider
from app.rag.schemas import RetrievedFaq
from app.rag.vector_store import QdrantFaqStore


class FaqRetrievalService:
    def __init__(
        self,
        embeddings: EmbeddingsProvider,
        store: QdrantFaqStore,
        top_k: int,
        score_threshold: float,
    ) -> None:
        self._embeddings = embeddings
        self._store = store
        self._top_k = top_k
        self._score_threshold = score_threshold

    async def retrieve(self, question: str) -> list[RetrievedFaq]:
        vector = await self._embeddings.embed_query(question)
        if len(vector) != self._embeddings.dimensions:
            raise ValueError("Embedding provider returned an unexpected vector dimension")
        return await self._store.search(vector, self._top_k, self._score_threshold)
