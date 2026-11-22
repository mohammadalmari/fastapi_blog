"""
Where uploaded files are kept.

Features talk to the ``Storage`` protocol, never to the disk, so that moving
files to an object store later (S3 and the like) means writing one more class
here and nothing else. Keys look like paths, ``avatars/3f2a....webp``.
"""

import secrets
from pathlib import Path
from typing import Annotated, Protocol

from asyncer import asyncify
from fastapi import Depends, Request


class Storage(Protocol):
    async def save(self, key: str, data: bytes) -> None:
        """Store the bytes under the key, replacing whatever was there."""
        ...

    async def read(self, key: str) -> bytes | None:
        """The bytes stored under the key, or None if there are none."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the key. Removing one that isn't there is not an error."""
        ...


class LocalStorage:
    """Files in a folder on this machine. Fine for one server; not for several."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        # The last line of defence against "../../etc/passwd" as a key. Keys
        # are made by the app, not taken from requests, but the one class
        # that touches the disk should not depend on every caller knowing.
        path = (self._root / key).resolve()
        if path == self._root or not path.is_relative_to(self._root):
            raise ValueError(f"Storage key {key!r} is outside the storage folder")
        return path

    # File access blocks, so each method hands its work to a thread.

    async def save(self, key: str, data: bytes) -> None:
        await asyncify(self._write)(self._path(key), data)

    async def read(self, key: str) -> bytes | None:
        return await asyncify(self._read)(self._path(key))

    async def delete(self, key: str) -> None:
        await asyncify(self._path(key).unlink)(missing_ok=True)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and then renamed into place: a rename is
        # atomic, so a reader sees the old file or the new one, never half.
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            # After a successful rename there is nothing here to remove. After
            # a failure there is half a file, on a disk that may be full.
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None


async def get_storage(request: Request) -> Storage:
    """The storage created with the app (see ``app.main``)."""
    return request.app.state.storage


StorageDep = Annotated[Storage, Depends(get_storage)]
