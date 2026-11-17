import logging
from hashlib import sha256
from hmac import new as new_hmac

from telegram import Update
from telegram.ext import ContextTypes

from app.agent.service import AgentService
from app.rag.answer_service import FALLBACK_MESSAGE

logger = logging.getLogger(__name__)


def conversation_id_for_update(
    update: Update,
    hmac_secret: str,
    key_id: str = "v1",
) -> str | None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None and update.message is not None:
        chat_id = getattr(update.message, "chat_id", None)
    if chat_id is None:
        return None
    digest = new_hmac(
        hmac_secret.encode("utf-8"),
        f"telegram-chat:{chat_id}".encode("utf-8"),
        sha256,
    ).hexdigest()
    return f"telegram:{key_id}:{digest}"


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return
    agent_service = context.application.bot_data.get("agent_service")
    if not isinstance(agent_service, AgentService):
        await update.message.reply_text(FALLBACK_MESSAGE)
        return
    try:
        hmac_secret = context.application.bot_data.get("thread_id_hmac_secret")
        key_id = context.application.bot_data.get("thread_id_hmac_key_id", "v1")
        if not isinstance(hmac_secret, str) or not hmac_secret:
            raise RuntimeError("Telegram thread identity is not configured")
        answer = await agent_service.handle_message(
            update.message.text,
            conversation_id=conversation_id_for_update(update, hmac_secret, str(key_id)),
        )
    except Exception as exc:
        logger.error(
            "Unhandled Telegram agent error",
            extra={"exception_type": type(exc).__name__},
        )
        answer = FALLBACK_MESSAGE
    await update.message.reply_text(answer)
