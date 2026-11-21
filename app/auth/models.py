from datetime import datetime

from pydantic import BaseModel

from app.core.types import PyObjectId


class RefreshToken(BaseModel):
    """One stored refresh token. ``id`` is the SHA-256 of the token itself."""

    id: str
    user_id: PyObjectId
    # Every token descended from one login shares a family. Rotation replaces
    # a token with a new one in the same family.
    family_id: PyObjectId
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: str | None = None
