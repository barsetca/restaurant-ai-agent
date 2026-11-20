from unittest.mock import AsyncMock

import httpx

from app.core.config import Settings
from app.main import create_app
from app.db.session import database_is_ready


class _Connection:
    async def execute(self, statement) -> None:
        assert str(statement) == "SELECT 1"


class _ConnectionContext:
    async def __aenter__(self) -> _Connection:
        return _Connection()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _AvailableEngine:
    def connect(self) -> _ConnectionContext:
        return _ConnectionContext()


class _UnavailableEngine:
    def connect(self) -> None:
        raise OSError("synthetic connection failure")


async def test_readiness_when_database_is_available(monkeypatch) -> None:
    readiness = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.health.database_is_ready", readiness)
    app = create_app(Settings(_env_file=None))
    app.state.db_engine = object()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    readiness.assert_awaited_once()


async def test_readiness_when_database_is_unavailable(monkeypatch) -> None:
    readiness = AsyncMock(return_value=False)
    monkeypatch.setattr("app.api.health.database_is_ready", readiness)
    app = create_app(Settings(_env_file=None))
    app.state.db_engine = object()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "dependency": "postgresql"}
    assert "postgresql+asyncpg" not in response.text


async def test_readiness_reports_checkpoint_dependency_without_secrets(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.api.health.database_is_ready",
        AsyncMock(return_value=True),
    )
    checkpoint_ready = AsyncMock(return_value=False)
    monkeypatch.setattr("app.api.health.checkpoint_is_ready", checkpoint_ready)
    app = create_app(Settings(_env_file=None))
    app.state.db_engine = object()
    app.state.checkpoint_required = True
    app.state.checkpointer = object()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependency": "checkpoint_postgresql",
    }
    assert "password" not in response.text
    checkpoint_ready.assert_awaited_once_with(app.state.checkpointer)


async def test_database_probe_available() -> None:
    assert await database_is_ready(_AvailableEngine()) is True


async def test_database_probe_unavailable() -> None:
    assert await database_is_ready(_UnavailableEngine()) is False
