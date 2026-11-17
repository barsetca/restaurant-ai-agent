from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from qdrant_client import AsyncQdrantClient, models

from app.rag.schemas import FaqEntry, RetrievedFaq

_METADATA_SOURCE_ID = "__faq_collection_metadata__"


class VectorStoreError(RuntimeError):
    pass


class VectorConfigurationError(VectorStoreError):
    pass


def point_id(source_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"restaurant-ai-agent/faq/{source_id}")


class QdrantFaqStore:
    def __init__(self, url: str, collection: str, timeout_seconds: float) -> None:
        self.collection = collection
        self._client = AsyncQdrantClient(url=url, timeout=max(1, int(timeout_seconds)))

    async def _write_metadata(self, model_name: str, dimensions: int) -> None:
        await self._client.upsert(
            collection_name=self.collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=point_id(_METADATA_SOURCE_ID),
                    vector=[0.0] * dimensions,
                    payload={
                        "record_type": "metadata",
                        "source_id": _METADATA_SOURCE_ID,
                        "embedding_model": model_name,
                        "embedding_dimensions": dimensions,
                    },
                )
            ],
        )

    async def ensure_collection(self, model_name: str, dimensions: int) -> None:
        metadata_id = point_id(_METADATA_SOURCE_ID)
        if not await self._client.collection_exists(self.collection):
            await self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
            )
            await self._write_metadata(model_name, dimensions)
            return

        info = await self._client.get_collection(self.collection)
        vectors = info.config.params.vectors
        actual_dimensions = getattr(vectors, "size", None)
        if actual_dimensions != dimensions:
            raise VectorConfigurationError(
                f"Qdrant collection vector size is {actual_dimensions}, expected {dimensions}"
            )
        metadata = await self._client.retrieve(
            collection_name=self.collection,
            ids=[metadata_id],
            with_payload=True,
            with_vectors=False,
        )
        if metadata:
            payload = metadata[0].payload or {}
            if payload.get("embedding_model") != model_name:
                raise VectorConfigurationError("Qdrant collection uses a different embedding model")
        else:
            await self._write_metadata(model_name, dimensions)

    async def list_hashes(self) -> dict[str, str]:
        result: dict[str, str] = {}
        offset = None
        faq_filter = models.Filter(
            must=[models.FieldCondition(key="record_type", match=models.MatchValue(value="faq"))]
        )
        while True:
            records, offset = await self._client.scroll(
                collection_name=self.collection,
                scroll_filter=faq_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                source_id = payload.get("source_id")
                content_hash = payload.get("content_hash")
                if isinstance(source_id, str) and isinstance(content_hash, str):
                    result[source_id] = content_hash
            if offset is None:
                return result

    async def upsert(self, entries: Sequence[FaqEntry], vectors: Sequence[list[float]]) -> None:
        if len(entries) != len(vectors):
            raise ValueError("entries and vectors length mismatch")
        points = [
            models.PointStruct(
                id=point_id(entry.source_id),
                vector=vector,
                payload={
                    "record_type": "faq",
                    "source_id": entry.source_id,
                    "category": entry.category,
                    "content_hash": entry.content_hash,
                    "question": entry.question,
                    "answer": entry.answer,
                    "updated_at": entry.updated_at,
                },
            )
            for entry, vector in zip(entries, vectors, strict=True)
        ]
        if points:
            await self._client.upsert(collection_name=self.collection, wait=True, points=points)

    async def delete_source_ids(self, source_ids: Sequence[str]) -> None:
        if source_ids:
            await self._client.delete(
                collection_name=self.collection,
                wait=True,
                points_selector=models.PointIdsList(points=[point_id(source_id) for source_id in source_ids]),
            )

    async def search(self, vector: list[float], top_k: int, score_threshold: float) -> list[RetrievedFaq]:
        response = await self._client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=models.Filter(
                must=[models.FieldCondition(key="record_type", match=models.MatchValue(value="faq"))]
            ),
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )
        results: list[RetrievedFaq] = []
        for point in response.points:
            payload = point.payload or {}
            try:
                entry = FaqEntry(
                    source_id=payload["source_id"],
                    category=payload["category"],
                    question=payload["question"],
                    answer=payload["answer"],
                    updated_at=payload.get("updated_at"),
                )
            except (KeyError, ValueError):
                continue
            results.append(RetrievedFaq(entry=entry, score=point.score))
        return results

    async def close(self) -> None:
        await self._client.close()
