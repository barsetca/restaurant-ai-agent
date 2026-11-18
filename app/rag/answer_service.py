import json
import logging
import re
from typing import Protocol

from langchain_openai import ChatOpenAI

from app.rag.prompts import FAQ_ANSWER_PROMPT
from app.rag.retrieval import FaqRetrievalService
from app.rag.schemas import AnswerDecision, RetrievedFaq

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "У меня пока нет достоверной информации по этому вопросу. "
    "Могу предложить обратиться к оператору ресторана"
)


class ContextAnswerer(Protocol):
    async def answer(self, question: str, context: list[RetrievedFaq]) -> AnswerDecision: ...


def build_context_json(context: list[RetrievedFaq], max_characters: int) -> str:
    items: list[dict[str, str]] = []
    size = 2
    for result in context:
        item = {
            "source_id": result.entry.source_id,
            "category": result.entry.category,
            "question": result.entry.question,
            "answer": result.entry.answer,
        }
        encoded = json.dumps(item, ensure_ascii=False)
        if size + len(encoded) > max_characters:
            break
        items.append(item)
        size += len(encoded) + 1
    return json.dumps(items, ensure_ascii=False)


def has_direct_conflict(context: list[RetrievedFaq]) -> bool:
    answers_by_question: dict[str, set[str]] = {}
    for result in context:
        question = re.sub(r"\W+", " ", result.entry.question.casefold()).strip()
        answer = re.sub(r"\s+", " ", result.entry.answer.casefold()).strip()
        answers_by_question.setdefault(question, set()).add(answer)
    return any(len(answers) > 1 for answers in answers_by_question.values())


class LangChainOpenAIAnswerer:
    def __init__(self, api_key: str, model_name: str, timeout_seconds: float, max_context_characters: int) -> None:
        model = ChatOpenAI(
            api_key=api_key,
            model=model_name,
            temperature=0,
            timeout=timeout_seconds,
            max_retries=2,
        )
        self._chain = FAQ_ANSWER_PROMPT | model.with_structured_output(
            AnswerDecision,
            method="json_schema",
            strict=True,
        )
        self._max_context_characters = max_context_characters

    async def answer(self, question: str, context: list[RetrievedFaq]) -> AnswerDecision:
        result = await self._chain.ainvoke(
            {
                "question": question,
                "context_json": build_context_json(context, self._max_context_characters),
            }
        )
        if not isinstance(result, AnswerDecision):
            raise TypeError("OpenAI returned an invalid FAQ decision")
        return result


class FaqAssistant:
    def __init__(self, retrieval: FaqRetrievalService, answerer: ContextAnswerer) -> None:
        self._retrieval = retrieval
        self._answerer = answerer

    async def answer(self, question: str) -> str:
        if not question.strip() or len(question) > 2000:
            return FALLBACK_MESSAGE
        try:
            context = await self._retrieval.retrieve(question)
            if not context or has_direct_conflict(context):
                return FALLBACK_MESSAGE
            decision = await self._answerer.answer(question, context)
            if decision.conflicting:
                return FALLBACK_MESSAGE
            if (
                not decision.supported
                and decision.clarification_needed
                and decision.clarification.strip()
            ):
                return decision.clarification.strip()
            if not decision.supported or not decision.answer.strip():
                return FALLBACK_MESSAGE
            return decision.answer.strip()
        except Exception:
            # Never log the raw user question or external credentials.
            logger.error("FAQ request failed")
            return FALLBACK_MESSAGE
