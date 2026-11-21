"""Liveness check for load balancers, container healthchecks and uptime monitors."""

from fastapi import APIRouter, HTTPException, status
from pymongo.errors import PyMongoError

from app.core.database import DbDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: DbDep) -> dict[str, str]:
    """Report 200 only if the app can also reach the database."""
    try:
        await db.command("ping")
    except PyMongoError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from error
    return {"status": "ok"}
