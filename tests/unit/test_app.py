import httpx
from contextlib import asynccontextmanager
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
    build_agent_service = Mock()
    monkeypatch.setattr("app.main.create_engine", create_engine)
    monkeypatch.setattr("app.main.build_telegram_application", build_telegram_application)
    monkeypatch.setattr("app.main.build_agent_service", build_agent_service)

    settings = Settings(_env_file=None, telegram_enabled=False, telegram_bot_token=None)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert app.state.db_engine is engine
        assert app.state.telegram_application is None
        assert app.state.agent_service is None
        create_engine.assert_called_once_with(settings.database_url)
        engine.dispose.assert_not_awaited()

    engine.dispose.assert_awaited_once_with()
    build_telegram_application.assert_not_called()
    build_agent_service.assert_not_called()


async def test_telegram_without_faq_builds_safe_agent_once(monkeypatch) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    agent_service = Mock()
    telegram_application = Mock()
    create_engine = Mock(return_value=engine)
    build_agent_service = Mock(return_value=agent_service)
    build_telegram_application = Mock(return_value=telegram_application)
    start_polling = AsyncMock()
    stop_polling = AsyncMock()
    build_faq_runtime = Mock()
    checkpointer = object()
    checkpoint_closed = False

    @asynccontextmanager
    async def open_checkpoint_saver(database_url):
        nonlocal checkpoint_closed
        assert database_url == settings.database_url
        try:
            yield checkpointer
        finally:
            checkpoint_closed = True

    checkpoint_ready = AsyncMock(return_value=True)
    monkeypatch.setattr("app.main.create_engine", create_engine)
    monkeypatch.setattr("app.main.build_agent_service", build_agent_service)
    monkeypatch.setattr("app.main.build_telegram_application", build_telegram_application)
    monkeypatch.setattr("app.main.start_polling", start_polling)
    monkeypatch.setattr("app.main.stop_polling", stop_polling)
    monkeypatch.setattr("app.main.build_faq_runtime", build_faq_runtime)
    monkeypatch.setattr("app.main.open_checkpoint_saver", open_checkpoint_saver)
    monkeypatch.setattr("app.main.checkpoint_is_ready", checkpoint_ready)

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="123456:synthetic-token",
        thread_id_hmac_secret="synthetic-hmac-secret-at-least-32-characters",
        faq_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert app.state.agent_service is agent_service
        assert app.state.telegram_application is telegram_application

    build_faq_runtime.assert_not_called()
    assert build_agent_service.call_count == 1
    assert build_agent_service.call_args.args == (None, checkpointer)
    assert build_agent_service.call_args.kwargs["order_dialog"].__class__.__name__ == "OrderDialog"
    build_telegram_application.assert_called_once_with(
        "123456:synthetic-token",
        agent_service,
        thread_id_hmac_secret="synthetic-hmac-secret-at-least-32-characters",
        thread_id_hmac_key_id="v1",
    )
    start_polling.assert_awaited_once_with(telegram_application)
    stop_polling.assert_awaited_once_with(telegram_application)
    checkpoint_ready.assert_awaited_once_with(checkpointer)
    assert checkpoint_closed is True


async def test_telegram_startup_fails_closed_when_checkpoint_schema_is_unavailable(
    monkeypatch,
) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    checkpointer = object()

    @asynccontextmanager
    async def open_checkpoint_saver(database_url):
        del database_url
        yield checkpointer

    monkeypatch.setattr("app.main.create_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.main.open_checkpoint_saver", open_checkpoint_saver)
    monkeypatch.setattr(
        "app.main.checkpoint_is_ready",
        AsyncMock(return_value=False),
    )
    build_telegram_application = Mock()
    monkeypatch.setattr(
        "app.main.build_telegram_application",
        build_telegram_application,
    )

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="123456:synthetic-token",
        thread_id_hmac_secret="synthetic-hmac-secret-at-least-32-characters",
    )
    app = create_app(settings)

    try:
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan must not start")
    except RuntimeError as exc:
        assert "checkpoint schema" in str(exc)

    build_telegram_application.assert_not_called()
    engine.dispose.assert_awaited_once_with()
