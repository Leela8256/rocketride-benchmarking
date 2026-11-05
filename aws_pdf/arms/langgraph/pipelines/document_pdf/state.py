"""Graph state for document-pdf-v1. Media rides as a path, never as bytes."""

from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class PdfState(TypedDict, total=False):
    source_path: str
    text: str
    chunks: List[Dict[str, Any]]
    vectors: List[List[float]]
    timings_ns: Dict[str, int]
    result: Optional[Dict[str, Any]]
