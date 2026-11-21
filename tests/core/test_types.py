import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.types import PyObjectId

HEX = "665f1c2e8a4b3d0012345678"


class Item(BaseModel):
    id: PyObjectId


def test_accepts_a_hex_string_and_an_object_id() -> None:
    assert Item(id=HEX).id == ObjectId(HEX)
    assert Item(id=ObjectId(HEX)).id == ObjectId(HEX)


@pytest.mark.parametrize("value", ["", "abc", "z" * 24, HEX + "00", 123, None])
def test_rejects_anything_else(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(PyObjectId).validate_python(value)


def test_json_gets_a_string_and_python_keeps_the_object_id() -> None:
    item = Item(id=ObjectId(HEX))

    assert item.model_dump_json() == f'{{"id":"{HEX}"}}'
    assert item.model_dump(mode="json") == {"id": HEX}
    # What a repository passes to Mongo must stay a real ObjectId.
    assert item.model_dump() == {"id": ObjectId(HEX)}


def test_openapi_schema_describes_a_string() -> None:
    schema = Item.model_json_schema()["properties"]["id"]

    assert schema["type"] == "string"
    assert schema["pattern"] == "^[0-9a-f]{24}$"


@pytest.mark.anyio
async def test_works_as_a_path_parameter_and_in_a_response() -> None:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def read_item(item_id: PyObjectId) -> Item:
        assert isinstance(item_id, ObjectId)
        return Item(id=item_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get(f"/items/{HEX}")
        bad = await client.get("/items/not-an-id")

    assert ok.status_code == 200
    assert ok.json() == {"id": HEX}
    assert bad.status_code == 422
