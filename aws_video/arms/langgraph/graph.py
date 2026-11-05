"""The compiled LangGraph for video-detect-v1.

Node names mirror the RocketRide detect pipe's COMPUTE components
one-for-one:
    frames   ~ frame_grabber_1  (interval 15 s)
    detect   ~ detect_1         (rfdetr, threshold 0.3)
    chunk    ~ preprocessor_1   (RecursiveCharacterTextSplitter 4000)
    embed    ~ embedding_1      (multi-qa-MiniLM-L6-cos-v1, 384-dim)
    assemble ~ response_1       (assembly half; transport is the server)

RocketRide's transport (webhook, MIME routing) maps to the FastAPI layer,
exactly as in the PDF arm: matched measurement boundaries, not matched
internal topology. No checkpointer — stateless request/response.
"""

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from workload.chunk import chunk_lines
from workload.detect import detect_frames
from workload.embed import embed_chunks
from workload.frames import cleanup_frames, extract_frames


class VideoState(TypedDict, total=False):
    video_path: str
    frames: list            # PNG paths on disk (streamed into detection)
    frames_dir: str
    det_lines: list[str]
    chunks: list[str]
    embeddings: list[list[float]]
    documents: list[dict]
    timings: dict


def frames_node(state: VideoState) -> dict:
    t0 = time.perf_counter()
    frames_dir, frames = extract_frames(state["video_path"])
    return {"frames": frames, "frames_dir": frames_dir,
            "timings": {**state.get("timings", {}),
                        "frames_s": round(time.perf_counter() - t0, 3)}}


def detect_node(state: VideoState) -> dict:
    t0 = time.perf_counter()
    try:
        lines = detect_frames(state["frames"])
    finally:
        cleanup_frames(state["frames_dir"])
    return {"det_lines": lines, "frames": [], "frames_dir": "",
            "timings": {**state["timings"],
                        "detect_s": round(time.perf_counter() - t0, 3)}}


def chunk_node(state: VideoState) -> dict:
    t0 = time.perf_counter()
    chunks = chunk_lines(state["det_lines"])
    return {"chunks": chunks,
            "timings": {**state["timings"],
                        "chunk_s": round(time.perf_counter() - t0, 3)}}


def embed_node(state: VideoState) -> dict:
    t0 = time.perf_counter()
    vecs = embed_chunks(state["chunks"])
    return {"embeddings": vecs,
            "timings": {**state["timings"],
                        "embed_s": round(time.perf_counter() - t0, 3)}}


def assemble_node(state: VideoState) -> dict:
    docs = [{"page_content": c, "embedding": v, "metadata": {"chunkId": i}}
            for i, (c, v) in enumerate(zip(state["chunks"], state["embeddings"]))]
    return {"documents": docs}


def build_video_graph():
    b = StateGraph(VideoState)
    b.add_node("frames", frames_node)
    b.add_node("detect", detect_node)
    b.add_node("chunk", chunk_node)
    b.add_node("embed", embed_node)
    b.add_node("assemble", assemble_node)
    b.add_edge(START, "frames")
    b.add_edge("frames", "detect")
    b.add_edge("detect", "chunk")
    b.add_edge("chunk", "embed")
    b.add_edge("embed", "assemble")
    b.add_edge("assemble", END)
    return b.compile()
