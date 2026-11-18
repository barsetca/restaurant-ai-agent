import logging
from time import perf_counter

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.agent.service import AgentResponse, AgentService
from app.bot.handlers import (
    _response_markup,
    agent_response,
    order_callback_handler,
    safe_send_reply,
    telegram_error_handler,
    text_message_handler,
)

logger = logging.getLogger(__name__)
_REQUEST_STARTED_AT = "restaurant_ai_request_started_at"


async def _telegram_request_started(request: httpx.Request) -> None:
    request.extensions[_REQUEST_STARTED_AT] = perf_counter()


async def _telegram_response_received(response: httpx.Response) -> None:
    started_at = response.request.extensions.get(_REQUEST_STARTED_AT)
    duration_ms = round((perf_counter() - started_at) * 1000, 2) if started_at else None
    endpoint = f"/{response.request.url.path.rsplit('/', 1)[-1]}"
    logger.info(
        "Telegram HTTP request completed",
        extra={
            "http_method": response.request.method,
            "endpoint": endpoint,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    application = getattr(context, "application", None)
    bot_data = getattr(application, "bot_data", {})
    if isinstance(bot_data.get("agent_service"), AgentService):
        result = await agent_response(update, context, "/start")
    else:
        result = AgentResponse("Restaurant AI Agent is running.", None, None, None)
    await safe_send_reply(
        update.message,
        result.text,
        reply_markup=_response_markup(result),
        operation="start_response",
    )


def build_telegram_application(
    token: str,
    agent_service: AgentService | None = None,
    *,
    thread_id_hmac_secret: str | None = None,
    thread_id_hmac_key_id: str = "v1",
) -> Application:
    event_hooks = {
        "request": [_telegram_request_started],
        "response": [_telegram_response_received],
    }
    application = (
        Application.builder()
        .token(token)
        .request(HTTPXRequest(httpx_kwargs={"event_hooks": event_hooks}))
        .get_updates_request(
            HTTPXRequest(connection_pool_size=1, httpx_kwargs={"event_hooks": event_hooks})
        )
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    if agent_service is not None:
        application.bot_data["agent_service"] = agent_service
        application.bot_data["thread_id_hmac_secret"] = thread_id_hmac_secret
        application.bot_data["thread_id_hmac_key_id"] = thread_id_hmac_key_id
        application.add_handler(
            CallbackQueryHandler(
                order_callback_handler,
                pattern=r"^order:[a-z_]+:[0-9]+(?::[A-Za-z0-9_-]{12})?$",
            )
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    application.add_error_handler(telegram_error_handler)
    return application


async def start_polling(application: Application) -> None:
    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable")
    await application.updater.start_polling()
    logger.info("Telegram long polling started")


async def stop_polling(application: Application) -> None:
    if application.updater and application.updater.running:
        await application.updater.stop()
    if application.running:
        await application.stop()
    await application.shutdown()
    logger.info("Telegram long polling stopped")
