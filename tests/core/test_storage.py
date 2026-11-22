"""LocalStorage against a temporary folder. No app."""

import os
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager

from app.core.settings import Settings
from app.core.storage import LocalStorage
from app.main import create_app

pytestmark = pytest.mark.anyio


async def test_what_is_saved_can_be_read_back_and_deleted(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "media")

    await storage.save("avatars/a.webp", b"picture")
    read_back = await storage.read("avatars/a.webp")
    await storage.delete("avatars/a.webp")

    assert read_back == b"picture"
    assert await storage.read("avatars/a.webp") is None
    # Nothing is left behind, the temporary file used while writing included.
    assert list((tmp_path / "media" / "avatars").iterdir()) == []


async def test_saving_again_replaces_the_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    await storage.save("a", b"old")

    await storage.save("a", b"new")

    assert await storage.read("a") == b"new"


async def test_missing_keys_are_none_and_deleting_them_is_fine(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    assert await storage.read("avatars/nothing.webp") is None
    await storage.delete("avatars/nothing.webp")


@pytest.mark.parametrize("key", ["../outside", "avatars/../../outside", "/etc/passwd", "", "."])
async def test_a_key_that_leaves_the_folder_is_refused(tmp_path: Path, key: str) -> None:
    """The attack: a key built from something a user sent, pointing elsewhere on the disk."""
    root = tmp_path / "media"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"secret")
    storage = LocalStorage(root)

    with pytest.raises(ValueError, match="outside the storage folder"):
        await storage.read(key)
    with pytest.raises(ValueError, match="outside the storage folder"):
        await storage.save(key, b"overwritten")
    with pytest.raises(ValueError, match="outside the storage folder"):
        await storage.delete(key)

    assert (tmp_path / "outside").read_bytes() == b"secret"


async def test_a_write_that_fails_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a full disk, half-written files must not pile up and make it fuller."""
    storage = LocalStorage(tmp_path)

    def no_space(self: Path, target: Path) -> Path:
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "replace", no_space)

    with pytest.raises(OSError, match="No space left"):
        await storage.save("avatars/a.webp", b"picture")

    assert list((tmp_path / "avatars").iterdir()) == []


async def test_a_usable_folder_passes_the_startup_check_and_keeps_nothing(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "media")

    await storage.check()

    assert list((tmp_path / "media").iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write anywhere")
async def test_a_folder_that_cannot_be_written_to_fails_the_startup_check(tmp_path: Path) -> None:
    """A mounted volume owned by another user: found at startup, not at the first upload."""
    locked = tmp_path / "media"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="FASTAPI_MEDIA_DIR"):
            await LocalStorage(locked).check()
    finally:
        locked.chmod(0o700)


async def test_the_app_refuses_to_start_when_its_media_folder_is_unusable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check exists; this proves starting the app runs it."""

    async def unusable(self: LocalStorage) -> None:
        raise RuntimeError("The media folder can't be written to")

    monkeypatch.setattr(LocalStorage, "check", unusable)

    with pytest.raises(RuntimeError, match="media folder"):
        async with LifespanManager(create_app(settings)):
            pass
