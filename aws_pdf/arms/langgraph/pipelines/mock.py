"""mock-v1 — the Phase 1 pipeline that proves the generic contract.

Deliberately NOT a LangGraph graph: Phase 1 forbids importing langgraph
(see the handoff's hard prohibitions). The real compiled graph arrives in
M2 and will satisfy `ainvoke` natively, replacing only this class — the
server does not change.

Deterministic on input bytes so integration tests can assert EXACT
canonical output bytes.
"""

import hashlib
import time
from pathlib import Path
from typing import Any, Dict

from service.errors import ServiceError
from service.pipeline import RequestContext


class MockPipeline:
    name = "mock-v1"

    async def warmup(self) -> None:
        """Trivial — nothing to load."""
        return None

    async def prepare_input(self, context: RequestContext) -> Dict[str, Any]:
        if context.source_path is None:
            raise ServiceError(
                "empty_input",
                "pipeline 'mock-v1' requires a file",
                context.request_id,
            )
        if not context.size_bytes:
            raise ServiceError(
                "empty_input", "uploaded file is empty", context.request_id
            )
        return {
            "request_id": context.request_id,
            "source_path": context.source_path,
            "options": context.options,
            "timings_ns": {},
        }

    async def ainvoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        start = time.perf_counter_ns()
        data = Path(state["source_path"]).read_bytes()
        result = {
            "kind": "mock",
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "options_echo": state.get("options", {}),
        }
        timings = dict(state.get("timings_ns") or {})
        timings["graph"] = time.perf_counter_ns() - start
        return {**state, "result": result, "timings_ns": timings}

    async def extract_output(self, final_state: Dict[str, Any]) -> Dict[str, Any]:
        return final_state["result"]


def build_mock_pipeline() -> MockPipeline:
    return MockPipeline()
