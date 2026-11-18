from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI

from app.agent.checkpoint import checkpoint_is_ready, open_checkpoint_saver
from app.agent.service import build_agent_service
from app.api.health import router as health_router
from app.bot.bootstrap import build_telegram_application, start_polling, stop_polling
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine
from app.mcp.client import MenuMcpClient
from app.orders.dialog import OrderDialog
from app.orders.intent import LlmOrderIntentExtractor
from app.orders.settings import OrderSettingsRepository
from app.rag.runtime import build_faq_runtime


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    database_password = urlsplit(app_settings.database_url).password
    configure_logging(
        app_settings.log_level,
        secrets=(
            app_settings.telegram_bot_token,
            app_settings.openai_api_key,
            app_settings.thread_id_hmac_secret,
            database_password,
            unquote(database_password) if database_password else None,
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db_engine = create_engine(app_settings.database_url)
        app.state.telegram_application = None
        app.state.faq_runtime = None
        app.state.agent_service = None
        app.state.checkpointer = None
        app.state.checkpoint_required = app_settings.telegram_enabled
        checkpoint_stack = AsyncExitStack()
        try:
            if app_settings.faq_enabled:
                app.state.faq_runtime = build_faq_runtime(app_settings)
            if app_settings.telegram_enabled:
                app.state.checkpointer = await checkpoint_stack.enter_async_context(
                    open_checkpoint_saver(app_settings.database_url)
                )
                if not await checkpoint_is_ready(app.state.checkpointer):
                    raise RuntimeError(
                        "LangGraph checkpoint schema is unavailable; "
                        "run the explicit checkpoint setup command"
                    )
                intent_extractor = (
                    LlmOrderIntentExtractor(
                        app_settings.openai_api_key,
                        app_settings.openai_chat_model,
                        app_settings.faq_request_timeout_seconds,
                    )
                    if app_settings.openai_api_key
                    else None
                )

                def menu_factory() -> MenuMcpClient:
                    return MenuMcpClient(
                        app_settings.database_url,
                        startup_timeout_seconds=app_settings.mcp_startup_timeout_seconds,
                        tool_timeout_seconds=app_settings.mcp_tool_timeout_seconds,
                        shutdown_timeout_seconds=app_settings.mcp_shutdown_timeout_seconds,
                        log_level=app_settings.log_level,
                    )

                order_dialog = OrderDialog(
                    menu_factory,
                    OrderSettingsRepository(app.state.db_engine),
                    intent_extractor,
                )
                app.state.agent_service = build_agent_service(
                    app.state.faq_runtime.assistant if app.state.faq_runtime else None,
                    app.state.checkpointer,
                    order_dialog=order_dialog,
                )
                telegram_application = build_telegram_application(
                    app_settings.telegram_bot_token or "",
                    app.state.agent_service,
                    thread_id_hmac_secret=app_settings.thread_id_hmac_secret,
                    thread_id_hmac_key_id=app_settings.thread_id_hmac_key_id,
                )
                app.state.telegram_application = telegram_application
                await start_polling(telegram_application)
            yield
        finally:
            if app.state.telegram_application is not None:
                await stop_polling(app.state.telegram_application)
            if app.state.faq_runtime is not None:
                await app.state.faq_runtime.close()
            await checkpoint_stack.aclose()
            await app.state.db_engine.dispose()

    application = FastAPI(title="Restaurant AI Agent", lifespan=lifespan)
    application.include_router(health_router)
    return application


app = create_app()
