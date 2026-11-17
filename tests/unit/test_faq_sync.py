from unittest.mock import AsyncMock

import pytest

from app.rag.schemas import FaqEntry, FaqSourceResult
from app.rag.sync_service import EmptyFaqSourceError, FaqSyncService


def _entry(source_id: str, answer: str = "Ответ") -> FaqEntry:
    return FaqEntry(source_id=source_id, category="general", question=f"Вопрос {source_id}", answer=answer)


def _dependencies(entries, existing):
    source = AsyncMock()
    source.load.return_value = FaqSourceResult(entries=entries, inactive=1, skipped_empty=2)
    embeddings = AsyncMock()
    embeddings.model_name = "test-model"
    embeddings.dimensions = 3
    embeddings.embed_documents.return_value = [[1.0, 0.0, 0.0] for _ in entries]
    store = AsyncMock()
    store.list_hashes.return_value = existing
    return source, embeddings, store


class FaultInjectingStore:
    def __init__(self, hashes, *, fail_upsert_once=False, fail_delete_once=False):
        self.hashes = dict(hashes)
        self.metadata_present = True
        self.fail_upsert_once = fail_upsert_once
        self.fail_delete_once = fail_delete_once
        self.upsert_calls = 0
        self.delete_calls = 0

    async def ensure_collection(self, model_name, dimensions):
        assert (model_name, dimensions) == ("test-model", 3)

    async def list_hashes(self):
        return dict(self.hashes)

    async def upsert(self, entries, vectors):
        assert len(entries) == len(vectors)
        self.upsert_calls += 1
        if self.fail_upsert_once:
            self.fail_upsert_once = False
            if entries:
                self.hashes[entries[0].source_id] = entries[0].content_hash
            raise RuntimeError("synthetic partial Qdrant upsert failure")
        self.hashes.update({entry.source_id: entry.content_hash for entry in entries})

    async def delete_source_ids(self, source_ids):
        self.delete_calls += 1
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("synthetic Qdrant delete failure")
        for source_id in source_ids:
            self.hashes.pop(source_id, None)


async def test_sync_adds_updates_removes_and_skips_unchanged() -> None:
    unchanged = _entry("same")
    updated = _entry("updated", "Новый ответ")
    added = _entry("added")
    source, embeddings, store = _dependencies(
        [unchanged, updated, added],
        {"same": unchanged.content_hash, "updated": "old-hash", "removed": "old-hash"},
    )
    embeddings.embed_documents.return_value = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    result = await FaqSyncService(source, embeddings, store).sync()

    assert (result.added, result.updated, result.removed, result.skipped_unchanged) == (1, 1, 1, 1)
    embeddings.embed_documents.assert_awaited_once()
    upserted = store.upsert.await_args.args[0]
    assert {entry.source_id for entry in upserted} == {"updated", "added"}
    store.delete_source_ids.assert_awaited_once_with(["removed"])


async def test_repeated_sync_does_not_embed_or_duplicate() -> None:
    entry = _entry("same")
    source, embeddings, store = _dependencies([entry], {"same": entry.content_hash})
    result = await FaqSyncService(source, embeddings, store).sync()
    assert result.skipped_unchanged == 1
    embeddings.embed_documents.assert_not_awaited()
    store.upsert.assert_not_awaited()


async def test_embedding_failure_does_not_delete_stale_points() -> None:
    entry = _entry("new")
    source, embeddings, store = _dependencies([entry], {"old": "hash"})
    embeddings.embed_documents.side_effect = RuntimeError("synthetic OpenAI failure")
    with pytest.raises(RuntimeError):
        await FaqSyncService(source, embeddings, store).sync()
    store.delete_source_ids.assert_not_awaited()


async def test_empty_source_refuses_to_clear_existing_index() -> None:
    source, embeddings, store = _dependencies([], {"one": "hash", "two": "hash"})

    with pytest.raises(EmptyFaqSourceError, match="refusing to remove"):
        await FaqSyncService(source, embeddings, store).sync()

    embeddings.embed_documents.assert_not_awaited()
    store.upsert.assert_not_awaited()
    store.delete_source_ids.assert_not_awaited()


async def test_allow_empty_source_clears_only_faq_points_and_repeat_is_safe() -> None:
    source, embeddings, _ = _dependencies([], {})
    store = FaultInjectingStore({"one": "hash", "two": "hash"})
    service = FaqSyncService(source, embeddings, store)

    cleared = await service.sync(allow_empty_source=True)
    repeated = await service.sync()

    assert cleared.removed == 2
    assert repeated.removed == 0
    assert store.hashes == {}
    assert store.metadata_present is True
    assert store.delete_calls == 2
    embeddings.embed_documents.assert_not_awaited()


async def test_upsert_failure_never_deletes_stale_points() -> None:
    new = _entry("new")
    source, embeddings, store = _dependencies([new], {"old": "old-hash"})
    store.upsert.side_effect = RuntimeError("synthetic Qdrant upsert failure")

    with pytest.raises(RuntimeError):
        await FaqSyncService(source, embeddings, store).sync()

    store.delete_source_ids.assert_not_awaited()


async def test_repeat_recovers_after_partial_upsert_failure() -> None:
    first = _entry("first")
    second = _entry("second")
    source, embeddings, _ = _dependencies([first, second], {})
    embeddings.embed_documents.side_effect = [
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        [[0.0, 1.0, 0.0]],
    ]
    store = FaultInjectingStore({"old": "old-hash"}, fail_upsert_once=True)
    service = FaqSyncService(source, embeddings, store)

    with pytest.raises(RuntimeError):
        await service.sync()
    recovered = await service.sync()

    assert recovered.added == 1
    assert recovered.removed == 1
    assert store.hashes == {"first": first.content_hash, "second": second.content_hash}
    assert store.metadata_present is True
    assert store.upsert_calls == 2


async def test_repeat_recovers_after_delete_failure_without_reembedding() -> None:
    new = _entry("new")
    source, embeddings, _ = _dependencies([new], {})
    store = FaultInjectingStore({"old": "old-hash"}, fail_delete_once=True)
    service = FaqSyncService(source, embeddings, store)

    with pytest.raises(RuntimeError):
        await service.sync()
    recovered = await service.sync()

    assert recovered.skipped_unchanged == 1
    assert recovered.removed == 1
    assert store.hashes == {"new": new.content_hash}
    assert store.metadata_present is True
    assert store.delete_calls == 2
    embeddings.embed_documents.assert_awaited_once()
