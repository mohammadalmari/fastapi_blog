from datetime import datetime

from pydantic import BaseModel, Field

from app.core.types import PyObjectId


class Role(BaseModel):
    """
    Every stored role is read through this model, on every request of every
    user who holds it. A field added later therefore needs a default (or a
    backfill): a required field missing from an old document would turn all
    of those requests into 500s. Fields it doesn't name are ignored.
    """

    id: PyObjectId
    name: str
    description: str = ""
    # Plain strings, not `Perm`: a stored role may briefly name a permission
    # that a new version of the code has removed, until startup cleans it up.
    permissions: list[str] = Field(default_factory=list)
    # Built in and relied on by the app: can't be deleted or renamed.
    is_system: bool = False
    # The role a new account is given. Exactly one role has this.
    is_default: bool = False
    created_at: datetime
    updated_at: datetime
