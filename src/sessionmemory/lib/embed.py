"""Turn note text into vectors, in-process and without a daemon.

`fastembed` runs `nomic-embed-text-v1.5` on ONNX Runtime inside this process. The
alternative considered was Ollama, and it was rejected for one reason: it is a service,
and a service is something every note write would depend on being up. A model file on
disk cannot be down.

The model cache is pinned rather than left at fastembed's default, which lives under the
system temp directory. macOS purges that periodically, and a silent re-download of 520MB
in the middle of `sessionmemory new` is indistinguishable from a hang.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastembed import TextEmbedding

MODEL_CODE = "nomic-embed-text-v1.5"
MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
DIM = 768

# nomic is trained with task prefixes and fastembed 0.8 does not add them, so they are
# added here, which the memoryfield spec permits for a model that mandates them.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

CACHE_ENV_VAR = "SESSIONMEMORY_MODEL_CACHE"
DEFAULT_CACHE = Path.home() / ".cache" / "sessionmemory" / "models"


class Embedder(Protocol):
    """Anything that turns pages and queries into vectors of `dim` floats.

    `name` is the model code the index file is named for, so two embedders with
    different names never share an index.
    """

    name: str
    dim: int

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per page text, in the order given."""
        ...  # pragma: no cover

    def encode_query(self, text: str) -> list[float]:
        """Return the vector for one search query."""
        ...  # pragma: no cover


class FastEmbedder:
    """Embeds with the real model, loaded on first use rather than at construction."""

    def __init__(self, cache_dir: Path = DEFAULT_CACHE) -> None:
        self.name = MODEL_CODE
        self.dim = DIM
        self.cache_dir = cache_dir
        self._model: TextEmbedding | None = None

    def _load(self) -> TextEmbedding:
        if self._model is None:
            from fastembed import TextEmbedding

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(self.cache_dir))
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [vector.tolist() for vector in self._load().embed(texts)]

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per page text, in the order given."""
        return self._encode([f"{DOCUMENT_PREFIX}{text}" for text in texts])

    def encode_query(self, text: str) -> list[float]:
        """Return the vector for one search query."""
        return self._encode([f"{QUERY_PREFIX}{text}"])[0]


class StubEmbedder:
    """Embeds deterministically from a hash, with no model and no network."""

    def __init__(self) -> None:
        self.name = "stub"
        self.dim = DIM

    def _one(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per page text, in the order given."""
        return [self._one(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        """Return the vector for one search query."""
        return self._one(text)


def default_embedder() -> FastEmbedder:
    """Build the embedder commands use, reading the model cache location from the environment.

    Returns:
        FastEmbedder: An embedder caching to `SESSIONMEMORY_MODEL_CACHE`, or
            `DEFAULT_CACHE` when that variable is unset or empty.
    """
    raw = os.environ.get(CACHE_ENV_VAR)
    if not raw:
        return FastEmbedder(cache_dir=DEFAULT_CACHE)
    return FastEmbedder(cache_dir=Path(raw).expanduser())
