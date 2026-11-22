"""What the generated API reference says about the app as a whole."""

import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.anyio


async def test_every_section_of_the_api_docs_is_described(app: FastAPI) -> None:
    schema = app.openapi()
    described = {tag["name"] for tag in schema["tags"]}
    used = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }

    assert used == described
    # One version, in one place people look and one place tools look.
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert schema["info"]["version"] == project["project"]["version"] == "1.0.0"


async def test_operation_ids_are_the_section_and_the_function_name(app: FastAPI) -> None:
    schema = app.openapi()
    ids = [
        operation["operationId"]
        for operations in schema["paths"].values()
        for operation in operations.values()
    ]

    # Client generators name their functions after these, so they are part of
    # what the API promises: short, and never two the same.
    assert schema["paths"]["/api/v1/tags"]["get"]["operationId"] == "posts-list_tags"
    assert len(ids) == len(set(ids))
    assert all("api_v1" not in operation_id for operation_id in ids)
