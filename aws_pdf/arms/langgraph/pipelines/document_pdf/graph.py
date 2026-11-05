"""The compiled LangGraph for document-pdf-v1.

Node names mirror the RocketRide pipe's COMPUTE components one-for-one:
    extract  ~ parse_1
    chunk    ~ preprocessor_1
    embed    ~ embedding_1
    assemble ~ response_1 (assembly half; the transport half is the server)

RocketRide's transport components (webhook, MIME routing) are deliberately
NOT nodes here — they map to the FastAPI layer and the adapter. Matched
measurement boundaries, not matched internal topology.

No checkpointer: this is a stateless request/response pipeline.
"""

from langgraph.graph import END, START, StateGraph

from pipelines.document_pdf.nodes import (
    assemble_node,
    chunk_node,
    embed_node,
    extract_node,
)
from pipelines.document_pdf.state import PdfState


def build_pdf_graph():
    b = StateGraph(PdfState)
    b.add_node("extract", extract_node)
    b.add_node("chunk", chunk_node)
    b.add_node("embed", embed_node)
    b.add_node("assemble", assemble_node)
    b.add_edge(START, "extract")
    b.add_edge("extract", "chunk")
    b.add_edge("chunk", "embed")
    b.add_edge("embed", "assemble")
    b.add_edge("assemble", END)
    return b.compile()
