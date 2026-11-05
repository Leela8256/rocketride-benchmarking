"""LangGraph video-detect service — the LangGraph arm of the video benchmark.

FastAPI shell around graph.py, mirroring the PDF arm's service contract:

    POST /process            multipart video file -> documents JSON
    GET  /health/ready       200 only after models are loaded AND a warmup
                             frame has been through the full graph
    GET  /meta               arm identity for provenance

Run:  uvicorn service:app --host 0.0.0.0 --port 8200
"""

import hashlib
import os
import tempfile
import time
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from graph import build_video_graph

ARM = "langgraph-video-detect-v1"
_graph = None
_ready = False
_PORT = os.environ.get("LG_PORT", "8200")
_TORCH_THREADS = os.environ.get("LG_TORCH_THREADS")
_TORCH_INTEROP = os.environ.get("LG_TORCH_INTEROP")
# anyio's default worker pool is 40 threads, which silently caps in-flight
# graph runs in ONE process. 0/unset = leave the default alone.
_THREAD_LIMIT = int(os.environ.get("LG_THREAD_LIMIT", "0") or 0)
_ckpt_hashes: dict = {}


def _configure_torch():
    """Matched posture only (env-driven; the default posture leaves torch
    alone). set_num_interop_threads must run before any parallel torch work,
    so this happens first thing in lifespan, before the models load."""
    if not (_TORCH_THREADS or _TORCH_INTEROP):
        return
    import torch
    if _TORCH_INTEROP:
        try:
            torch.set_num_interop_threads(int(_TORCH_INTEROP))
        except RuntimeError as exc:      # already initialised — report, don't hide
            print(f"[service] set_num_interop_threads: {exc}", flush=True)
    if _TORCH_THREADS:
        torch.set_num_threads(int(_TORCH_THREADS))


def _checkpoint_hashes() -> dict:
    """sha256 of the model files actually on disk (best effort, cached)."""
    if _ckpt_hashes:
        return _ckpt_hashes
    import glob
    cands = {"rfdetr": glob.glob(os.path.expanduser("~/.roboflow/models/*.pth"))
                       + glob.glob("/root/.cache/**/rf-detr*.pth", recursive=True),
             "minilm": glob.glob("/root/.cache/huggingface/hub/models--sentence-transformers--multi-qa-MiniLM-L6-cos-v1/snapshots/*/model.safetensors")
                       + glob.glob("/root/.cache/huggingface/hub/models--sentence-transformers--multi-qa-MiniLM-L6-cos-v1/snapshots/*/pytorch_model.bin")}
    for k, paths in cands.items():
        for p in sorted(set(paths))[:1]:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            _ckpt_hashes[k] = {"path": p, "sha256": h.hexdigest()}
    return _ckpt_hashes


def _warmup():
    """Load both models and push one synthetic frame through everything —
    a cold model must never be paid for inside a measured request."""
    global _ready
    from PIL import Image
    from workload.chunk import chunk_lines
    from workload.detect import detect_frame
    from workload.embed import embed_chunks
    line = detect_frame(Image.new("RGB", (352, 288)))
    embed_chunks(chunk_lines([line or "[]"]))
    _ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    _configure_torch()
    if _THREAD_LIMIT:
        anyio.to_thread.current_default_thread_limiter().total_tokens = _THREAD_LIMIT
    _graph = build_video_graph()
    _warmup()
    _checkpoint_hashes()
    yield


app = FastAPI(title=ARM, lifespan=lifespan)


@app.get("/health/ready")
def ready():
    if not _ready:
        return JSONResponse({"ready": False}, status_code=503)
    return {"ready": True}


@app.get("/meta")
def meta():
    from workload.chunk import CHUNK_OVERLAP, CHUNK_SIZE
    from workload.detect import THRESHOLD
    from workload.embed import MODEL_ID
    from workload.frames import INTERVAL_S
    from workload import detect as _det, embed as _emb
    torch_rb = None
    try:
        import torch
        torch_rb = {"num_threads": torch.get_num_threads(),
                    "num_interop_threads": torch.get_num_interop_threads(),
                    "version": torch.__version__}
    except Exception as exc:
        torch_rb = {"error": str(exc)[:80]}
    ck = _checkpoint_hashes()
    return {"arm": ARM,
            "pid": os.getpid(), "port": _PORT,
            "worker_index": os.environ.get("LG_WORKER_INDEX"),
            "graph_compiled": _graph is not None,
            "rfdetr_loaded": getattr(_det, "_model", None) is not None,
            "minilm_loaded": getattr(_emb, "_model", None) is not None,
            "rfdetr_checkpoint_sha256": (ck.get("rfdetr") or {}).get("sha256"),
            "minilm_checkpoint_sha256": (ck.get("minilm") or {}).get("sha256"),
            "torch": torch_rb,
            "env": {k: os.environ.get(k) for k in
                    ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                     "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS",
                     "MALLOC_ARENA_MAX")},
            "detect_concurrency_per_process": _det.DETECT_CONCURRENCY,
            "frame_interval_s": INTERVAL_S,
            "detector": {"model": "rfdetr-base", "threshold": THRESHOLD},
            "splitter": {"chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP},
            "embedding": {"model": MODEL_ID, "dim": 384}}


def _process_sync(path: str) -> dict:
    t0 = time.perf_counter()
    state = _graph.invoke({"video_path": path, "timings": {}})
    docs = state["documents"]
    return {
        "arm": ARM,
        "documents": docs,
        "n_frames": len(state["det_lines"]),
        "n_chunks": len(docs),
        "total_chars": sum(len(d["page_content"]) for d in docs),
        "output_sha256": hashlib.sha256(
            "".join(d["page_content"] for d in docs).encode()).hexdigest(),
        "timings": {**state["timings"],
                    "total_s": round(time.perf_counter() - t0, 3)},
    }


@app.post("/process")
async def process(file: UploadFile):
    if not _ready:
        raise HTTPException(503, "not warmed up")
    suffix = os.path.splitext(file.filename or "video")[1] or ".avi"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        while chunk := await file.read(1 << 22):
            tf.write(chunk)
        tmp = tf.name
    try:
        # CPU-bound graph in a worker thread so the event loop stays live.
        result = await anyio.to_thread.run_sync(_process_sync, tmp)
        result["filename"] = file.filename
        result["worker_pid"] = os.getpid()
        result["port"] = _PORT
        return result
    finally:
        os.unlink(tmp)
