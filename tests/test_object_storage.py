import pytest

from app.services import (
    InvalidObjectKeyError,
    LocalObjectStorage,
    ObjectNotFoundError,
    ObjectStorage,
)


@pytest.fixture
def store(tmp_path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path / "objects")


def test_implements_interface(store):
    assert isinstance(store, ObjectStorage)


def test_put_then_get_roundtrip(store):
    store.put_object("raw/abc", b"\x00binary\xffdata")
    assert store.get_object("raw/abc") == b"\x00binary\xffdata"


def test_files_land_under_the_key_path(store):
    store.put_object("processed/x.json", b"{}")
    assert (store.root / "processed" / "x.json").read_bytes() == b"{}"


def test_put_overwrites_atomically_and_leaves_no_temp_files(store):
    store.put_object("raw/k", b"one")
    store.put_object("raw/k", b"two")
    assert store.get_object("raw/k") == b"two"
    assert [p.name for p in (store.root / "raw").iterdir()] == ["k"]


def test_get_missing_raises_not_found(store):
    with pytest.raises(ObjectNotFoundError):
        store.get_object("raw/missing")


def test_get_on_a_directory_key_is_not_found(store):
    store.put_object("raw/k", b"x")
    with pytest.raises(ObjectNotFoundError):
        store.get_object("raw")


def test_delete_is_idempotent(store):
    store.put_object("raw/k", b"x")
    store.delete_object("raw/k")
    store.delete_object("raw/k")
    with pytest.raises(ObjectNotFoundError):
        store.get_object("raw/k")


def test_empty_object_is_allowed(store):
    store.put_object("raw/empty", b"")
    assert store.get_object("raw/empty") == b""


def test_rejects_non_bytes(store):
    with pytest.raises(TypeError):
        store.put_object("raw/k", "text")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "../outside", "raw/../../outside", "a//b", "raw/.hidden", ".x", "a b", "a\\b", "x" * 600, "raw/\x00"],
)
def test_rejects_unsafe_keys(store, key):
    with pytest.raises(InvalidObjectKeyError):
        store.put_object(key, b"x")
    with pytest.raises(InvalidObjectKeyError):
        store.get_object(key)


def test_symlink_cannot_escape_the_root(tmp_path, store):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (store.root / "link").symlink_to(outside)
    with pytest.raises(InvalidObjectKeyError):
        store.get_object("link/secret")
