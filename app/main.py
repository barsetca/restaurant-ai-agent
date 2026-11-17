from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.bot.bootstrap import build_telegram_application, start_polling, stop_polling
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db_engine = create_engine(app_settings.database_url)
        app.state.telegram_application = None
        try:
            if app_settings.telegram_enabled:
                telegram_application = build_telegram_application(app_settings.telegram_bot_token or "")
                app.state.telegram_application = telegram_application
                await start_polling(telegram_application)
            yield
        finally:
            if app.state.telegram_application is not None:
                await stop_polling(app.state.telegram_application)
            await app.state.db_engine.dispose()

    application = FastAPI(title="Restaurant AI Agent", lifespan=lifespan)
    application.include_router(health_router)
    return application


app = create_app()
