import json
import math

from app.worker.embedding import CHUNK_SIZE, DIMENSIONS, generate_embeddings, serialize


def gen(data: bytes, doc="d1", name="f.txt"):
    return generate_embeddings(document_id=doc, filename=name, data=data)


def test_output_is_deterministic():
    data = b"the same bytes" * 1000
    assert serialize(gen(data)) == serialize(gen(data))


def test_different_content_gives_different_vectors():
    a, b = gen(b"alpha" * 50), gen(b"bravo" * 50)
    assert a["chunks"][0]["embedding"] != b["chunks"][0]["embedding"]
    assert a["source"]["sha256"] != b["source"]["sha256"]


def test_vectors_have_fixed_dimension_and_unit_length():
    result = gen(b"x" * (CHUNK_SIZE * 3 + 5))
    assert result["dimensions"] == DIMENSIONS
    for chunk in result["chunks"]:
        vec = chunk["embedding"]
        assert len(vec) == DIMENSIONS
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, abs_tol=1e-4)


def test_chunking_boundaries():
    assert gen(b"")["chunk_count"] == 0
    assert gen(b"x" * CHUNK_SIZE)["chunk_count"] == 1
    assert gen(b"x" * (CHUNK_SIZE + 1))["chunk_count"] == 2
    result = gen(b"x" * (CHUNK_SIZE + 10))
    assert [c["length"] for c in result["chunks"]] == [CHUNK_SIZE, 10]
    assert [c["offset"] for c in result["chunks"]] == [0, CHUNK_SIZE]


def test_serialization_is_canonical_json():
    raw = serialize(gen(b"hello"))
    parsed = json.loads(raw)
    assert parsed["model"] == "vectorpipe-fake-embedding-v1"
    assert raw == json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
