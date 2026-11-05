"""Chunking. Pure computation — no fastapi, no langgraph imports.

PINNED, DO NOT PARAMETERIZE. RecursiveCharacterTextSplitter with PURE
library defaults (chunk_size=4000, chunk_overlap=200, length_function=len)
applied to `text + "\\n"`.

This mirrors the RocketRide engine's REAL behavior: its preprocessor's
configured chunk size is ignored (filed bug — and Phase R confirmed the
schema exposes no chunk-size field at all), so the engine runs library
defaults. Matching that is what makes this "the same work". Never expose
these as request options; never "fix" them to 512 or 2048.
"""

from typing import Any, Dict, List

CHUNK_SIZE = 4000
CHUNK_OVERLAP = 200


def _splitter():
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    # Constructed with NO arguments on purpose: pure library defaults.
    return RecursiveCharacterTextSplitter()


def effective_split_config() -> Dict[str, Any]:
    """What the splitter will actually use — asserted in tests."""
    s = _splitter()
    return {
        "chunk_size": s._chunk_size,
        "chunk_overlap": s._chunk_overlap,
        "length_function": getattr(s._length_function, "__name__", str(s._length_function)),
    }


def split_document(text: str) -> List[Dict[str, Any]]:
    """Split into chunk records: {"index", "text", "start", "end"}.

    Offsets are into the split input (`text + "\\n"`), located in order so
    repeated chunk text still yields monotonically increasing offsets.
    """
    payload = text + "\n"
    pieces = _splitter().split_text(payload)

    chunks: List[Dict[str, Any]] = []
    cursor = 0
    for i, piece in enumerate(pieces):
        start = payload.find(piece, cursor)
        if start < 0:  # splitter stripped whitespace at a boundary
            start = payload.find(piece)
        if start < 0:
            start = cursor
        end = start + len(piece)
        chunks.append({"index": i, "text": piece, "start": start, "end": end})
        cursor = max(cursor, start + 1)
    return chunks
