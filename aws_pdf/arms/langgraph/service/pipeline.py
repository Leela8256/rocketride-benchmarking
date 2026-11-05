"""The generic pipeline contract.

FastAPI owns transport; PipelineRegistry owns discovery; pipeline adapters
translate requests into graph-specific state. Nothing in service/ may know
anything about a pipeline's topology or modality — that is what lets ANY
compiled graph (sequential, branching, fan-out, loops, agent) drop in later
without redesigning the server.

Input validation lives in prepare_input, PER PIPELINE. The server does not
decide what a pipeline accepts.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class RequestContext:
    """HTTP-side facts about one request. NOT graph state."""

    request_id: str
    pipeline: str
    source_path: Optional[str] = None
    filename: Optional[str] = None
    media_type: Optional[str] = None
    size_bytes: Optional[int] = None
    options: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Pipeline(Protocol):
    """What the server requires of anything registered as a pipeline."""

    name: str

    async def warmup(self) -> None:
        """Load whatever must be hot before the server reports ready."""
        ...

    async def prepare_input(self, context: RequestContext) -> Dict[str, Any]:
        """Validate the request and build this pipeline's initial state.

        Raise ServiceError with unsupported_media_type / empty_input as
        appropriate — the server does not pre-judge the input.
        """
        ...

    async def ainvoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Run the pipeline. A compiled LangGraph graph satisfies this natively."""
        ...

    async def extract_output(self, final_state: Dict[str, Any]) -> Dict[str, Any]:
        """Return the dict that becomes the response "output" field, unmodified."""
        ...


PIPELINE_METHODS = ("warmup", "prepare_input", "ainvoke", "extract_output")
