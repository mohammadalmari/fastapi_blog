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
