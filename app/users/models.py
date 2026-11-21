"""The user as the app handles it internally. Never returned by a route as is."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.types import PyObjectId


class User(BaseModel):
    """
    Every stored user document is read through this model, old ones included.
    A field added later therefore needs a default (or a backfill of existing
    documents): a required field missing from an old document is a 500.
    """

    id: PyObjectId
    email: str
    username: str
    password_hash: str
    display_name: str | None = None
    bio: str | None = None
    avatar_key: str | None = None
    role_ids: list[PyObjectId] = Field(default_factory=list)
    is_active: bool = True
    created_at: datetime
    updated_at: datetime
