from fastapi import APIRouter, status

# Import rule between these two features: auth may import anything from users,
# but users imports only `auth.dependencies`. Keeping it one-directional is
# what prevents a circular import.
from app.auth.dependencies import CurrentUserDep
from app.core.errors import error_responses
from app.users.models import User
from app.users.schemas import UserMe

router = APIRouter(prefix="/users", tags=["users"])


# `response_model` rather than a return type: the function returns the internal
# User, and FastAPI filters it down to UserMe, dropping `password_hash`.
@router.get(
    "/me",
    response_model=UserMe,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED),
)
async def read_me(user: CurrentUserDep) -> User:
    """The account the access token belongs to."""
    return user
