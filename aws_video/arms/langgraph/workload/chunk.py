"""Chunking. Pure computation — no fastapi, no langgraph imports.

Mirrors RocketRide's preprocessor_langchain (default profile,
RecursiveCharacterTextSplitter, strlen): frame JSON lines joined by
newlines, split at 4000 characters, no overlap. Parameters recovered
empirically from the ES2016d capture (4000/0 reproduces the engine's
20 chunks byte-exactly; 4096 and 3600 do not).
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = 4000
CHUNK_OVERLAP = 0

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)


def chunk_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    return _splitter.split_text("\n".join(lines))
