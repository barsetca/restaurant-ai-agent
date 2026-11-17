import os
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient

from app.rag.schemas import FaqEntry, FaqSourceResult
from app.rag.sync_service import EmptyFaqSourceError, FaqSyncService
from app.rag.vector_store import QdrantFaqStore, VectorConfigurationError


class MutableSource:
    def __init__(self, entries):
        self.entries = entries

    async def load(self):
        return FaqSourceResult(entries=self.entries)


class SemanticFakeEmbeddings:
    model_name = "synthetic-semantic-v1"
    dimensions = 3

    def __init__(self):
        self.document_calls = 0

    @staticmethod
    def _vector(text):
        lowered = text.casefold()
        if any(word in lowered for word in ("закры", "времени", "работ")):
            return [1.0, 0.0, 0.0]
        if "достав" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed_documents(self, texts):
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    async def embed_query(self, text):
        return self._vector(text)


def _qdrant_url():
    url = os.getenv("TEST_QDRANT_URL", "")
    if not url:
        pytest.skip("TEST_QDRANT_URL is not set")
    return url


@pytest.mark.integration
async def test_qdrant_sync_update_delete_and_semantic_search():
    url = _qdrant_url()
    collection = f"faq_test_{uuid4().hex}"
    store = QdrantFaqStore(url, collection, timeout_seconds=10)
    cleanup_client = AsyncQdrantClient(url=url)
    source = MutableSource(
        [
            FaqEntry(source_id="hours", category="hours", question="Когда вы закрываетесь?", answer="В 22:00"),
            FaqEntry(source_id="delivery", category="delivery", question="Есть доставка?", answer="Да"),
        ]
    )
    embeddings = SemanticFakeEmbeddings()
    service = FaqSyncService(source, embeddings, store)
    try:
        initial = await service.sync()
        assert initial.added == 2
        repeated = await service.sync()
        assert repeated.skipped_unchanged == 2
        assert embeddings.document_calls == 1

        source.entries = [
            FaqEntry(source_id="hours", category="hours", question="Когда вы закрываетесь?", answer="В 23:00")
        ]
        changed = await service.sync()
        assert (changed.updated, changed.removed) == (1, 1)

        query = await embeddings.embed_query("До какого времени вы работаете?")
        results = await store.search(query, top_k=3, score_threshold=0.8)
        assert len(results) == 1
        assert results[0].entry.answer == "В 23:00"
    finally:
        await store.close()
        if await cleanup_client.collection_exists(collection):
            await cleanup_client.delete_collection(collection)
        await cleanup_client.close()


@pytest.mark.integration
async def test_qdrant_rejects_embedding_model_mismatch():
    url = _qdrant_url()
    collection = f"faq_test_{uuid4().hex}"
    store = QdrantFaqStore(url, collection, timeout_seconds=10)
    cleanup_client = AsyncQdrantClient(url=url)
    try:
        await store.ensure_collection("synthetic-model-v1", 3)
        with pytest.raises(VectorConfigurationError, match="different embedding model"):
            await store.ensure_collection("synthetic-model-v2", 3)
    finally:
        await store.close()
        if await cleanup_client.collection_exists(collection):
            await cleanup_client.delete_collection(collection)
        await cleanup_client.close()


@pytest.mark.integration
async def test_qdrant_empty_source_requires_opt_in_and_preserves_metadata():
    url = _qdrant_url()
    collection = f"faq_test_{uuid4().hex}"
    store = QdrantFaqStore(url, collection, timeout_seconds=10)
    cleanup_client = AsyncQdrantClient(url=url)
    source = MutableSource(
        [FaqEntry(source_id="hours", category="hours", question="Когда вы открыты?", answer="С 10:00")]
    )
    embeddings = SemanticFakeEmbeddings()
    service = FaqSyncService(source, embeddings, store)
    try:
        await service.sync()
        source.entries = []

        with pytest.raises(EmptyFaqSourceError):
            await service.sync()
        assert set(await store.list_hashes()) == {"hours"}

        cleared = await service.sync(allow_empty_source=True)
        repeated = await service.sync()
        assert cleared.removed == 1
        assert repeated.removed == 0
        assert await store.list_hashes() == {}
        count = await cleanup_client.count(collection_name=collection, exact=True)
        assert count.count == 1  # Collection metadata point remains.
    finally:
        await store.close()
        if await cleanup_client.collection_exists(collection):
            await cleanup_client.delete_collection(collection)
        await cleanup_client.close()
