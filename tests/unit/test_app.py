import httpx
from unittest.mock import AsyncMock, Mock

from app.core.config import Settings
from app.main import create_app


def test_application_can_be_created() -> None:
    app = create_app(Settings(_env_file=None))
    assert app.title == "Restaurant AI Agent"


async def test_liveness() -> None:
    app = create_app(Settings(_env_file=None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_lifespan_creates_and_disposes_engine_without_telegram(monkeypatch) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    create_engine = Mock(return_value=engine)
    build_telegram_application = Mock()
    monkeypatch.setattr("app.main.create_engine", create_engine)
    monkeypatch.setattr("app.main.build_telegram_application", build_telegram_application)

    settings = Settings(_env_file=None, telegram_enabled=False, telegram_bot_token=None)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert app.state.db_engine is engine
        assert app.state.telegram_application is None
        create_engine.assert_called_once_with(settings.database_url)
        engine.dispose.assert_not_awaited()

    engine.dispose.assert_awaited_once_with()
    build_telegram_application.assert_not_called()
