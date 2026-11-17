from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.db.session import database_is_ready

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    if await database_is_ready(request.app.state.db_engine):
        return JSONResponse({"status": "ready"})
    return JSONResponse(
        {"status": "not_ready", "dependency": "postgresql"},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
