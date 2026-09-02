"""Tests for the deterministic stub embedder and the lazy real one."""

from __future__ import annotations

import pytest

from sessionmemory.lib import embed


def test_stub_embedder_returns_one_vector_per_document_of_length_dim():
    """Verify encode_documents returns one DIM-length vector per text."""
    vectors = embed.StubEmbedder().encode_documents(["alpha", "beta", "gamma"])

    assert len(vectors) == 3
    assert all(len(vector) == embed.DIM for vector in vectors)


def test_stub_embedder_query_is_one_vector():
    """Verify encode_query returns a single DIM-length vector."""
    assert len(embed.StubEmbedder().encode_query("alpha")) == embed.DIM


def test_stub_embedder_is_stable_across_calls_and_instances():
    """Verify the same text encodes to the same vector, across calls and instances."""
    text = "the quick brown fox"

    first = embed.StubEmbedder().encode_documents([text])[0]
    second = embed.StubEmbedder().encode_documents([text])[0]

    assert first == second


def test_stub_embedder_vectors_are_l2_normalized():
    """Verify every returned vector has unit L2 norm."""
    for vector in embed.StubEmbedder().encode_documents(["alpha", "beta"]):
        assert sum(value * value for value in vector) ** 0.5 == pytest.approx(1.0, abs=1e-9)


def test_stub_embedder_encode_empty_batch_returns_empty_list():
    """Verify an empty batch short-circuits to an empty list."""
    assert embed.StubEmbedder().encode_documents([]) == []


def test_fast_embedder_reports_model_code_without_loading_the_model():
    """Verify name is the spec's model code and the model is not loaded at construction."""
    fast = embed.FastEmbedder()

    assert fast.name == embed.MODEL_CODE == "nomic-embed-text-v1.5"
    assert fast.dim == embed.DIM == 768
    assert fast._model is None


def test_fast_embedder_prefixes_documents_and_queries(mocker):
    """Verify the nomic task prefixes are prepended, since fastembed does not."""
    fast = embed.FastEmbedder()
    model = mocker.Mock()
    model.embed.side_effect = lambda texts: [
        mocker.Mock(tolist=lambda: [0.0] * embed.DIM) for _ in texts
    ]
    mocker.patch.object(fast, "_load", return_value=model)

    fast.encode_documents(["page text"])
    fast.encode_query("q")

    model.embed.assert_any_call(["search_document: page text"])
    model.embed.assert_any_call(["search_query: q"])


def test_fast_embedder_encode_empty_batch_returns_empty_list_without_loading(mocker):
    """Verify an empty batch short-circuits before the model is ever loaded."""
    fast = embed.FastEmbedder()
    load = mocker.patch.object(fast, "_load")

    assert fast.encode_documents([]) == []
    load.assert_not_called()


def test_fast_embedder_load_creates_the_cache_dir_and_caches_the_model(tmp_path, mocker):
    """Verify _load creates the cache directory and loads the model once, then reuses it."""
    fake_model = mocker.Mock()
    text_embedding = mocker.patch("fastembed.TextEmbedding", return_value=fake_model)
    cache_dir = tmp_path / "models"
    fast = embed.FastEmbedder(cache_dir=cache_dir)

    first = fast._load()
    second = fast._load()

    assert first is fake_model
    assert second is fake_model
    text_embedding.assert_called_once_with(model_name=embed.MODEL_NAME, cache_dir=str(cache_dir))
    assert cache_dir.is_dir()


def test_default_embedder_uses_default_cache_when_env_var_is_unset(monkeypatch):
    """Verify the cache directory falls back to DEFAULT_CACHE with no env var set."""
    monkeypatch.delenv(embed.CACHE_ENV_VAR, raising=False)
    assert embed.default_embedder().cache_dir == embed.DEFAULT_CACHE


def test_default_embedder_uses_env_var_cache_directory_when_set(monkeypatch, tmp_path):
    """Verify a configured cache directory overrides the default."""
    monkeypatch.setenv(embed.CACHE_ENV_VAR, str(tmp_path / "models"))
    assert embed.default_embedder().cache_dir == tmp_path / "models"
