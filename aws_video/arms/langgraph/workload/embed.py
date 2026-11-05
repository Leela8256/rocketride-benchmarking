"""Embedding. Pure computation — no fastapi, no langgraph imports.

PINNED: sentence-transformers/multi-qa-MiniLM-L6-cos-v1, CPU, 384-dim,
L2-normalized. This is what RocketRide's `miniLM` profile actually resolves
to (services.json in the engine tree; independently confirmed by
reproducing the engine's ES2016d vectors to 1.06e-07). NOT
all-MiniLM-L6-v2 — that is the engine's `miniAll` profile, and using it
here would silently produce incomparable vectors that still pass every
dimension/norm check.

Lazy singleton, same pattern as the PDF arm's embed.py.
"""

import threading

MODEL_ID = "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"
EMBED_DIM = 384
_lock = threading.Lock()
_model = None


def _load():
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_ID, device="cpu")
    return _model


def embed_chunks(chunks: list[str]) -> list[list[float]]:
    if not chunks:
        return []
    model = _load()
    vecs = model.encode(chunks, normalize_embeddings=True)
    return [v.tolist() for v in vecs]
