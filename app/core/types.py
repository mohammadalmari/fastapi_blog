"""Types shared by the schemas of every feature."""

from typing import Annotated

from bson import ObjectId
from pydantic import PlainSerializer, PlainValidator, WithJsonSchema


def _to_object_id(value: object) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    raise ValueError("Not a valid id: expected 24 hexadecimal characters")


# MongoDB ids in Pydantic models. Pydantic doesn't know ObjectId, so this says
# how to read one (from an ObjectId or its 24-character hex string), how to
# write one to JSON (as that string) and how to describe it in the OpenAPI
# schema. `model_dump()` keeps the real ObjectId, which is what Mongo wants.
PyObjectId = Annotated[
    ObjectId,
    PlainValidator(_to_object_id),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": "^[0-9a-f]{24}$",
            "examples": ["665f1c2e8a4b3d0012345678"],
        }
    ),
]
