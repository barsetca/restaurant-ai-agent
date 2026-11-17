from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.agent.checkpoint import checkpoint_is_ready
from app.db.session import database_is_ready

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    if not await database_is_ready(request.app.state.db_engine):
        return JSONResponse(
            {"status": "not_ready", "dependency": "postgresql"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if getattr(request.app.state, "checkpoint_required", False) and (
        getattr(request.app.state, "checkpointer", None) is None
        or not await checkpoint_is_ready(request.app.state.checkpointer)
    ):
        return JSONResponse(
            {"status": "not_ready", "dependency": "checkpoint_postgresql"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return JSONResponse({"status": "ready"})
