from dataclasses import dataclass

from app.rag.embeddings import EmbeddingsProvider
from app.rag.sheets_source import FaqSource
from app.rag.vector_store import QdrantFaqStore


class EmptyFaqSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    added: int
    updated: int
    removed: int
    skipped_unchanged: int
    skipped_inactive: int
    skipped_empty: int
    errors: int = 0


class FaqSyncService:
    def __init__(self, source: FaqSource, embeddings: EmbeddingsProvider, store: QdrantFaqStore) -> None:
        self._source = source
        self._embeddings = embeddings
        self._store = store

    async def sync(self, *, allow_empty_source: bool = False) -> SyncResult:
        source_result = await self._source.load()
        await self._store.ensure_collection(self._embeddings.model_name, self._embeddings.dimensions)
        existing = await self._store.list_hashes()
        if not source_result.entries and existing and not allow_empty_source:
            raise EmptyFaqSourceError(
                "FAQ source has no active entries; refusing to remove the existing index. "
                "Use --allow-empty-source only for an intentional full FAQ cleanup."
            )
        current = {entry.source_id: entry for entry in source_result.entries}
        changed = [
            entry for entry in source_result.entries if existing.get(entry.source_id) != entry.content_hash
        ]
        added = sum(entry.source_id not in existing for entry in changed)
        updated = len(changed) - added
        unchanged = len(source_result.entries) - len(changed)
        removed_ids = sorted(set(existing) - set(current))

        if changed:
            vectors = await self._embeddings.embed_documents([entry.embedding_text for entry in changed])
            if any(len(vector) != self._embeddings.dimensions for vector in vectors):
                raise ValueError("Embedding provider returned an unexpected vector dimension")
            await self._store.upsert(changed, vectors)

        # Never delete old points until all embeddings and upserts have succeeded.
        await self._store.delete_source_ids(removed_ids)
        return SyncResult(
            added=added,
            updated=updated,
            removed=len(removed_ids),
            skipped_unchanged=unchanged,
            skipped_inactive=source_result.inactive,
            skipped_empty=source_result.skipped_empty,
        )
