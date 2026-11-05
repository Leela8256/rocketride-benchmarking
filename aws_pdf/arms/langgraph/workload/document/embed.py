"""Embedding. Pure computation — no fastapi, no langgraph imports.

PINNED: sentence-transformers/multi-qa-MiniLM-L6-cos-v1, CPU, 384-dim,
normalize_embeddings=True, float32 -> Python float.

Phase R verified the RocketRide engine resolves its `miniLM` profile to
this exact model id and returns L2-normalized 384-dim vectors, so both
arms embed with the same model.

Module-level model state is deliberate: several pipelines (document-v1 and
document-pdf-v1) share ONE loaded model rather than a copy each.
"""

import os
import threading
from typing import List

MODEL_ID = "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"
DEVICE = "cpu"
EMBED_DIM = 384
NORMALIZE = True

_model = None
_lock = threading.Lock()


def load_model():
    """Load (once) and return the shared model. Explicit, never lazy-in-node."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                # Respect HF_HUB_OFFLINE: when set, a cache miss must fail
                # loudly rather than silently reaching the network.
                _model = SentenceTransformer(MODEL_ID, device=DEVICE)
    return _model


def is_loaded() -> bool:
    return _model is not None


def model_info() -> dict:
    return {
        "model_id": MODEL_ID,
        "device": DEVICE,
        "dim": EMBED_DIM,
        "normalize": NORMALIZE,
        "loaded": is_loaded(),
        "hf_hub_offline": os.environ.get("HF_HUB_OFFLINE"),
    }


def embed_chunks(texts: List[str]) -> List[List[float]]:
    """Embed chunk texts -> list of 384-float vectors (plain Python floats)."""
    if not texts:
        return []
    model = load_model()
    arr = model.encode(
        texts,
        normalize_embeddings=NORMALIZE,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [[float(x) for x in row] for row in arr]
