"""document-pdf-v1 — Pipeline protocol adapter over the compiled graph."""

from pathlib import Path
from typing import Any, Dict

from pipelines.document_pdf.graph import build_pdf_graph
from service.errors import ServiceError
from service.pipeline import RequestContext
from workload.document.embed import load_model

PDF_MEDIA_TYPE = "application/pdf"
WARMUP_PDF = Path(__file__).resolve().parent / "fixtures" / "warmup.pdf"
WARMUP_PDF_SHA256 = "1914e13df37d238e5fa3823962db19dd21e87b02f0a159ec363664c93a2695ed"


class DocumentPdfPipeline:
    name = "document-pdf-v1"

    def __init__(self) -> None:
        # Compiled ONCE, at construction — never per request.
        self.graph = build_pdf_graph()

    async def warmup(self) -> None:
        """Load the model and run a real PDF through the FULL graph.

        Anything that fails here aborts startup, which is the point: a
        pipeline that cannot process its own fixture must never be
        advertised as ready.
        """
        load_model()
        state = {"source_path": str(WARMUP_PDF), "timings_ns": {}}
        final = await self.graph.ainvoke(state)
        result = final.get("result")
        if not result or not result.get("chunks") or not result.get("vectors"):
            raise RuntimeError(
                f"{self.name} warmup produced no chunks/vectors from {WARMUP_PDF}"
            )

    async def prepare_input(self, context: RequestContext) -> Dict[str, Any]:
        if context.source_path is None:
            raise ServiceError(
                "empty_input", f"pipeline {self.name!r} requires a file", context.request_id
            )
        if context.media_type != PDF_MEDIA_TYPE:
            raise ServiceError(
                "unsupported_media_type",
                f"pipeline {self.name!r} accepts only {PDF_MEDIA_TYPE}, "
                f"got {context.media_type!r}",
                context.request_id,
            )
        return {"source_path": context.source_path, "timings_ns": {}}

    async def ainvoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return await self.graph.ainvoke(state)

    async def extract_output(self, final_state: Dict[str, Any]) -> Dict[str, Any]:
        return final_state["result"]


def build_document_pdf_pipeline() -> DocumentPdfPipeline:
    return DocumentPdfPipeline()
