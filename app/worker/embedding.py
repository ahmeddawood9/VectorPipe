"""Deterministic fake "embedding" generation.

The real system would call an embedding model here. For the prototype we derive a stable pseudo-vector
per chunk from its SHA-256 digest, so identical input always yields byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import math

MODEL_NAME = "vectorpipe-fake-embedding-v1"
DIMENSIONS = 16
CHUNK_SIZE = 2048  # bytes per chunk


def _embed(chunk: bytes) -> tuple[bytes, list[float]]:
    digest = hashlib.sha256(chunk).digest()  # 32 bytes -> 16 unsigned 16-bit values
    raw = [int.from_bytes(digest[i : i + 2], "big") / 65535 * 2 - 1 for i in range(0, 32, 2)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return digest, [round(v / norm, 6) for v in raw]


def generate_embeddings(*, document_id: str, filename: str, data: bytes) -> dict:
    chunks = []
    for index, offset in enumerate(range(0, len(data), CHUNK_SIZE)):
        chunk = data[offset : offset + CHUNK_SIZE]
        digest, vector = _embed(chunk)
        chunks.append(
            {
                "index": index,
                "offset": offset,
                "length": len(chunk),
                "sha256": digest.hex()[:16],
                "embedding": vector,
            }
        )
    return {
        "document_id": document_id,
        "filename": filename,
        "model": MODEL_NAME,
        "dimensions": DIMENSIONS,
        "source": {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def serialize(result: dict) -> bytes:
    """Canonical JSON encoding so repeated runs write identical bytes."""
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
