"""A grown session re-embeds only its new chunks (no DB, no model).

Regression (2026-11): every update re-embedded the whole transcript, 5,000+
chunks for the largest sessions, though only the tail had changed.
"""

from types import SimpleNamespace

import pipeline


def _chunks(*texts):
    return [SimpleNamespace(content=t) for t in texts]


def test_unchanged_chunks_reuse_stored_vectors(monkeypatch):
    sent: list[str] = []

    def fake_batch(texts):
        sent.extend(texts)
        return [[9.0] for _ in texts]

    monkeypatch.setattr(pipeline, "embed_batch", fake_batch)
    reuse = {
        pipeline._content_digest("old a"): [1.0],
        pipeline._content_digest("old b"): [2.0],
    }

    vecs, reused = pipeline._embed_chunks(_chunks("old a", "old b", "new tail"), reuse)

    assert sent == ["new tail"]
    assert vecs == [[1.0], [2.0], [9.0]]
    assert reused == 2


def test_no_reuse_embeds_everything_in_order(monkeypatch):
    monkeypatch.setattr(pipeline, "embed_batch", lambda texts: [[float(len(t))] for t in texts])
    texts = [f"chunk {i:02d}" + "x" * i for i in range(23)]

    vecs, reused = pipeline._embed_chunks(_chunks(*texts))

    assert reused == 0
    assert vecs == [[float(len(t))] for t in texts]


def test_failed_batch_falls_back_per_chunk(monkeypatch):
    def boom(_texts):
        raise RuntimeError("ollama down")

    def one(text):
        if text == "bad":
            raise RuntimeError("bad chunk")
        return [5.0]

    monkeypatch.setattr(pipeline, "embed_batch", boom)
    monkeypatch.setattr(pipeline, "embed", one)

    vecs, _ = pipeline._embed_chunks(_chunks("good", "bad"))

    assert vecs[0] == [5.0]
    assert vecs[1] == [0.0] * 1024


def test_digest_matches_postgres_md5():
    # SELECT md5('héllo') on a UTF-8 database
    assert pipeline._content_digest("héllo") == "be50e8478cf24ff3595bc7307fb91b50"
