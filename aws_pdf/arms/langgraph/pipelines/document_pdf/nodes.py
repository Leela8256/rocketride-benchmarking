"""Thin graph nodes. Each calls ONE workload function and returns only its
own state update plus its timings_ns entry.

Nodes are PLAIN SYNC FUNCTIONS on purpose: LangGraph's async runtime
dispatches sync nodes to its default executor, keeping CPU-bound work off
the event loop. This is the documented pattern for compute nodes — the
PDF-1K run demonstrated what happens otherwise (async nodes calling sync
compute serialized every request through the loop and the server drowned
under concurrent load). No executor sizing, no max_concurrency: defaults.

No reformatting, no rounding, no reordering — whatever the workload returns
is what lands in state and, ultimately, in the response.
"""

import time
from typing import Any, Dict

from pipelines.document_pdf.state import PdfState
from workload.document.embed import embed_chunks
from workload.document.extract import extract
from workload.document.split import split_document


def _timed(state: PdfState, name: str, started: int) -> Dict[str, int]:
    timings = dict(state.get("timings_ns") or {})
    timings[name] = time.perf_counter_ns() - started
    return timings


def extract_node(state: PdfState) -> Dict[str, Any]:
    t0 = time.perf_counter_ns()
    text = extract(state["source_path"])
    return {"text": text, "timings_ns": _timed(state, "extract", t0)}


def chunk_node(state: PdfState) -> Dict[str, Any]:
    t0 = time.perf_counter_ns()
    chunks = split_document(state["text"])
    return {"chunks": chunks, "timings_ns": _timed(state, "chunk", t0)}


def embed_node(state: PdfState) -> Dict[str, Any]:
    t0 = time.perf_counter_ns()
    vectors = embed_chunks([c["text"] for c in state["chunks"]])
    return {"vectors": vectors, "timings_ns": _timed(state, "embed", t0)}


def assemble_node(state: PdfState) -> Dict[str, Any]:
    t0 = time.perf_counter_ns()
    result = {"chunks": state["chunks"], "vectors": state["vectors"]}
    return {"result": result, "timings_ns": _timed(state, "assemble", t0)}
