from unittest.mock import AsyncMock

from app.rag.retrieval import FaqRetrievalService


async def test_retrieval_passes_threshold_and_top_k() -> None:
    embeddings = AsyncMock()
    embeddings.dimensions = 3
    embeddings.embed_query.return_value = [1.0, 0.0, 0.0]
    store = AsyncMock()
    store.search.return_value = []
    service = FaqRetrievalService(embeddings, store, top_k=3, score_threshold=0.81)
    assert await service.retrieve("Перефразированный вопрос") == []
    store.search.assert_awaited_once_with([1.0, 0.0, 0.0], 3, 0.81)
