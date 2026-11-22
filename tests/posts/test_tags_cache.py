"""The list of tags, remembered for a while. Needs an app built with remembering on."""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.ttl import Remembered
from app.posts.repository import PostRepository
from app.users.models import User
from tests.conftest import MakePost
from tests.helpers import API

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def settings_overrides() -> dict[str, Any]:
    return {"tags_cache_seconds": 60}


@pytest.fixture(autouse=True)
def _nothing_remembered_yet(app: FastAPI) -> None:
    # This module's app is shared by its tests; each starts with nothing kept.
    app.state.tag_cache = Remembered(60)


async def test_tags_are_counted_once_and_then_remembered(
    client: AsyncClient,
    author: User,
    make_post: MakePost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_post(author, "First", tags=["python"], published=True)
    counted = 0
    real = PostRepository.tag_counts

    async def counting(self: PostRepository) -> object:
        nonlocal counted
        counted += 1
        return await real(self)

    monkeypatch.setattr(PostRepository, "tag_counts", counting)

    first = await client.get(f"{API}/tags")
    await make_post(author, "Second", tags=["python", "fastapi"], published=True)
    second = await client.get(f"{API}/tags")

    assert first.json() == [{"name": "python", "count": 1}]
    # A little behind, by design: the second post isn't counted yet.
    assert second.json() == first.json()
    assert counted == 1


async def test_a_remembered_list_tells_caches_how_long_it_is_good_for(client: AsyncClient) -> None:
    response = await client.get(f"{API}/tags")

    assert response.headers["cache-control"] == "public, max-age=60"
