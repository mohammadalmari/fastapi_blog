"""Every MongoDB query on the ``refresh_tokens`` collection."""

from datetime import datetime

from bson import ObjectId
from pymongo import IndexModel

from app.auth.models import RefreshToken
from app.core.database import Database, Document, Session, register_indexes, utcnow

COLLECTION = "refresh_tokens"

register_indexes(
    COLLECTION,
    [
        # A TTL index: MongoDB deletes each document once its `expires_at` has
        # passed, so expired tokens clean themselves up.
        IndexModel("expires_at", expireAfterSeconds=0),
        IndexModel("user_id"),
        IndexModel("family_id"),
    ],
)


class RefreshTokenRepository:
    def __init__(self, db: Database) -> None:
        self._tokens = db[COLLECTION]

    async def create(
        self,
        *,
        token_hash: str,
        user_id: ObjectId,
        family_id: ObjectId,
        expires_at: datetime,
        user_agent: str | None,
    ) -> None:
        doc: Document = {
            "_id": token_hash,
            "user_id": user_id,
            "family_id": family_id,
            "expires_at": expires_at,
            "revoked_at": None,
            "replaced_by": None,
            "created_at": utcnow(),
            "user_agent": user_agent,
        }
        await self._tokens.insert_one(doc)

    async def get(self, token_hash: str) -> RefreshToken | None:
        doc = await self._tokens.find_one({"_id": token_hash})
        return RefreshToken.model_validate({**doc, "id": doc["_id"]}) if doc else None

    async def rotate(self, token_hash: str, *, replaced_by: str) -> bool:
        """
        Retire a token in favour of its replacement. False if it was no
        longer active.

        The "still active" condition and the update are one atomic operation,
        so of two requests rotating the same token at the same moment exactly
        one gets True.
        """
        result = await self._tokens.update_one(
            {"_id": token_hash, "revoked_at": None},
            {"$set": {"revoked_at": utcnow(), "replaced_by": replaced_by}},
        )
        return result.modified_count == 1

    async def revoke_family(self, family_id: ObjectId) -> None:
        """End one session: every token descended from the same login."""
        await self._revoke_many({"family_id": family_id})

    async def revoke_all_for_user(
        self, user_id: ObjectId, *, session: Session | None = None
    ) -> None:
        """End every session of a user."""
        await self._revoke_many({"user_id": user_id}, session=session)

    async def _revoke_many(self, query: Document, *, session: Session | None = None) -> None:
        await self._tokens.update_many(
            {**query, "revoked_at": None},
            {"$set": {"revoked_at": utcnow()}},
            session=session,
        )
