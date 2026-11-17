from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.rag.answer_service import FaqAssistant, LangChainOpenAIAnswerer
from app.rag.embeddings import LangChainOpenAIEmbeddings
from app.rag.retrieval import FaqRetrievalService
from app.rag.sheets_source import GoogleSheetsFaqSource, JsonFaqSource
from app.rag.sync_service import FaqSyncService
from app.rag.vector_store import QdrantFaqStore


class FaqConfigurationError(ValueError):
    pass


def _required(value: str | None, name: str) -> str:
    if not value:
        raise FaqConfigurationError(f"{name} is required for FAQ")
    return value


@dataclass
class FaqRuntime:
    assistant: FaqAssistant
    store: QdrantFaqStore

    async def close(self) -> None:
        await self.store.close()


def _build_embeddings(settings: Settings) -> LangChainOpenAIEmbeddings:
    return LangChainOpenAIEmbeddings(
        api_key=_required(settings.openai_api_key, "OPENAI_API_KEY"),
        model_name=settings.openai_embedding_model,
        dimensions=settings.openai_embedding_dimensions,
        timeout_seconds=settings.faq_request_timeout_seconds,
    )


def _build_store(settings: Settings) -> QdrantFaqStore:
    return QdrantFaqStore(
        url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        timeout_seconds=settings.faq_request_timeout_seconds,
    )


def build_faq_runtime(settings: Settings) -> FaqRuntime:
    embeddings = _build_embeddings(settings)
    store = _build_store(settings)
    retrieval = FaqRetrievalService(
        embeddings=embeddings,
        store=store,
        top_k=settings.faq_retrieval_top_k,
        score_threshold=settings.faq_retrieval_score_threshold,
    )
    answerer = LangChainOpenAIAnswerer(
        api_key=_required(settings.openai_api_key, "OPENAI_API_KEY"),
        model_name=settings.openai_chat_model,
        timeout_seconds=settings.faq_request_timeout_seconds,
        max_context_characters=settings.faq_max_context_characters,
    )
    return FaqRuntime(assistant=FaqAssistant(retrieval, answerer), store=store)


def build_faq_sync_service(settings: Settings, fixture: Path | None = None) -> tuple[FaqSyncService, QdrantFaqStore]:
    source = (
        JsonFaqSource(fixture)
        if fixture is not None
        else GoogleSheetsFaqSource(
            spreadsheet_id=_required(
                settings.google_sheets_spreadsheet_id,
                "GOOGLE_SHEETS_SPREADSHEET_ID",
            ),
            credentials_path=_required(settings.google_credentials_path, "GOOGLE_CREDENTIALS_PATH"),
        )
    )
    embeddings = _build_embeddings(settings)
    store = _build_store(settings)
    return FaqSyncService(source, embeddings, store), store
