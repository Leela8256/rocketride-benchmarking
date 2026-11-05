"""The response envelope. Timings NEVER enter the body — headers carry them.

There is deliberately no fixed state TypedDict here: pipeline state shape is
graph-specific and is built by each adapter's prepare_input.
"""

from typing import Any, Dict, Optional

SCHEMA_VERSION = 1


def build_envelope(
    request_id: str,
    pipeline: str,
    filename: Optional[str],
    media_type: Optional[str],
    size_bytes: Optional[int],
    output: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "pipeline": pipeline,
        "input": {
            "filename": filename,
            "media_type": media_type,
            "size_bytes": size_bytes,
        },
        "output": output,
    }
