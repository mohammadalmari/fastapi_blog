"""LocalStorage against a temporary folder. No app."""

from pathlib import Path

import pytest

from app.core.storage import LocalStorage

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
