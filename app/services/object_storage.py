"""Object storage abstraction plus a filesystem implementation.

Application code depends only on :class:`ObjectStorage`. ``LocalObjectStorage`` keeps objects as files
under a root directory (``raw/...``, ``processed/...``). A cloud implementation can later be dropped in
behind the same three methods without touching API or worker logic.
"""

from __future__ import annotations

import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path


class ObjectStorageError(Exception):
    """Base class for storage failures."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested key does not exist."""


class InvalidObjectKeyError(ObjectStorageError, ValueError):
    """The key is not a safe, well-formed object key."""


class ObjectStorage(ABC):
    @abstractmethod
    def put_object(self, key: str, data: bytes) -> None:
        """Create or atomically overwrite ``key``."""

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Return the bytes stored at ``key`` or raise :class:`ObjectNotFoundError`."""

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Remove ``key``. Deleting a missing key is not an error (idempotent)."""


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._\-/]+$")
_MAX_KEY_LENGTH = 512


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, key: str) -> Path:
        if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LENGTH or not _KEY_PATTERN.match(key):
            raise InvalidObjectKeyError(f"invalid object key: {key!r}")
        parts = key.split("/")
        if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            raise InvalidObjectKeyError(f"invalid object key: {key!r}")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise InvalidObjectKeyError(f"object key escapes storage root: {key!r}")
        return path

    def put_object(self, key: str, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a hidden temp file in the same directory, then rename: readers never see a partial
        # object, and concurrent writers of the same key simply race to an identical final state.
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise ObjectStorageError(f"failed to write {key!r}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    def get_object(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFoundError(key) from exc
        except OSError as exc:
            raise ObjectStorageError(f"failed to read {key!r}: {exc}") from exc

    def delete_object(self, key: str) -> None:
        path = self._resolve(key)
        try:
            path.unlink(missing_ok=True)
        except (IsADirectoryError, PermissionError, OSError) as exc:
            raise ObjectStorageError(f"failed to delete {key!r}: {exc}") from exc
