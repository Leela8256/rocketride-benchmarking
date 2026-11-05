# How LangGraph is wrapped in FastAPI

RocketRide ships as an engine that serves a `.pipe` file. LangGraph ships as
a library. To compare them as production services, the LangGraph arm is a
FastAPI service we wrote around a compiled `StateGraph`, built so that only
the framework differs from the RocketRide pipe. This document explains the
shape, the rules that keep the two arms comparable, and where each piece
lives.

## 1. The three-layer rule

Both LangGraph services (PDF and video) are layered the same way:

```
+------------------------------------------------------------------+
| transport: FastAPI                                               |
|   POST /v1/process/<pipeline> (PDF) or POST /process (video)     |
|   GET /health/ready (warm-up gated), GET /meta (provenance)      |
|   maps to RocketRide's webhook + response_documents components   |
+------------------------------------------------------------------+
| orchestration: the compiled LangGraph StateGraph                 |
|   PDF:   START -> extract -> chunk -> embed -> assemble -> END   |
|   video: START -> frames -> detect -> chunk -> embed -> assemble |
+------------------------------------------------------------------+
| workload: pure computation, no fastapi or langgraph imports      |
|   PDF:   extract.py extract_pdf.py split.py embed.py             |
|   video: frames.py detect.py chunk.py embed.py                   |
+------------------------------------------------------------------+
```

The transport layer is the measurement boundary and is identical in role on
both arms (webhook in, documents out). The graph is the thing under test.
The workload is framework-free so the same functions could be run outside
any server to produce reference outputs.

RocketRide's transport components (webhook, MIME routing) are deliberately
not graph nodes. Matched measurement boundaries, not matched internal
topology.

## 2. Node-for-node mapping to the RocketRide pipe

PDF (`aws_pdf/pipe/benchmark_pdf.pipe` versus
`aws_pdf/arms/langgraph/pipelines/document_pdf/graph.py`):

| RocketRide component | config | LangGraph node | implementation |
|---|---|---|---|
| `webhook_1` | source | FastAPI handler | `service/app.py` |
| `parse_1` (`parse`) | engine-bundled Apache Tika 3.2.3 | `extract` | `workload/document/extract.py`: `EXTRACTOR=tika` calls an Apache Tika 3.2.3 sidecar loaded with RocketRide's own `tika-config.xml`; `EXTRACTOR=pypdf` uses pinned pypdf 6.15.0 |
| `preprocessor_1` (`preprocessor_langchain`) | profile default, RecursiveCharacterTextSplitter, strlen | `chunk` | `workload/document/split.py`: `RecursiveCharacterTextSplitter()` with pure library defaults, 4000 chars, 200 overlap, applied to `text + "\n"`. The engine ignores its configured chunk size and runs library defaults, so this matches real behaviour |
| `embedding_1` (`embedding_transformer`) | profile `miniLM` | `embed` | `workload/document/embed.py`: `sentence-transformers/multi-qa-MiniLM-L6-cos-v1`, CPU, 384-d, L2-normalised |
| `response_1` (`response_documents`) | lane `documents` | `assemble` + response envelope | `service/schemas.py`, `service/canonical.py` |

Video (`aws_video/pipe/benchmark_video_detect.pipe` versus
`aws_video/arms/langgraph/graph.py`):

| RocketRide component | config | LangGraph node | implementation |
|---|---|---|---|
| `webhook_1` | source, `video` lane | FastAPI handler | `service.py` |
| `frame_grabber_1` | interval 15 s | `frames` | `workload/frames.py`: `ffmpeg -vf fps=1/15`, lossless PNG, frames streamed from disk one at a time |
| `detect_1` (`detect`) | profile `rfdetr`, threshold 0.3 | `detect` | `workload/detect.py`: `rfdetr.RFDETRBase`, one JSON line per frame in the engine's exact format |
| `preprocessor_1` | default profile | `chunk` | `workload/chunk.py`: `RecursiveCharacterTextSplitter(4000, 0)` over newline-joined frame lines. 4000/0 reproduces the engine's chunks byte-exactly; 4096 and 3600 do not |
| `embedding_1` | profile `miniLM` | `embed` | `workload/embed.py`: same MiniLM model as PDF |
| `response_1` | lane `documents` | `assemble` | `graph.py` |

Two parameter facts that matter more than anything else in this table:

- The engine's `miniLM` profile is `multi-qa-MiniLM-L6-cos-v1`, not
  `all-MiniLM-L6-v2` (that one is the engine's `miniAll` profile). The wrong
  model passes every structural check (384-d, unit norm) and is silently
  incomparable. Verified by reproducing engine vectors to 1e-7.
- PDF chunking is 4000/200 (library defaults); video chunking is 4000/0.
  Both were recovered from engine output, not from documentation.

## 3. The PDF service in detail (`aws_pdf/arms/langgraph/`)

```
service/
  app.py        FastAPI app factory: ready check -> registry lookup -> persist upload
                -> adapter.prepare_input -> graph.ainvoke -> adapter.extract_output
                -> canonical JSON bytes -> Response; temp file deleted in finally
  pipeline.py   the Pipeline protocol: warmup, prepare_input, ainvoke, extract_output
  registry.py   name -> pipeline; validates the protocol at startup, not on first request
  config.py     typed settings from env; pins OMP/MKL/OPENBLAS threads before torch loads
  canonical.py  canonical JSON encoding + sha256 (the X-Output-SHA256 header)
  schemas.py    the response envelope; timings never enter the body
  upload.py     streams the multipart upload to a temp file with a size cap
  errors.py     ServiceError -> JSON error response with a code
pipelines/
  document_pdf/ graph.py nodes.py state.py adapter.py fixtures/warmup.pdf
  mock.py       a no-op pipeline for transport tests
workload/document/
  extract.py extract_pdf.py split.py embed.py
```

Design decisions, each with its reason:

- **Nodes are plain sync functions.** LangGraph's async runtime dispatches
  sync nodes to its default executor, keeping CPU-bound work off the event
  loop. Async nodes calling sync compute serialised every request through the
  loop and drowned the server under load (the first 1k-document run).
- **No executor sizing, no checkpointer.** The graph is stateless
  request/response. `EXECUTOR_WORKERS` is reported on `/meta` but inert: the
  real executor width is LangGraph's default `min(32, cpu_count + 4)`, and
  `cpu_count()` sees host cores, not the cgroup quota. `MAX_INFLIGHT_REQUESTS`
  and `REQUEST_TIMEOUT_SECONDS` are likewise reported but never enforced.
  Never quote `/meta` for concurrency; provenance says so explicitly.
- **Warm-up gates readiness.** `warmup()` loads the model and runs a real PDF
  through the full graph before `/health/ready` returns 200. A pipeline that
  cannot process its own fixture is never advertised as ready, and cold model
  loads can never land inside a measured request.
- **Identity in headers.** Every response carries `X-Request-ID`,
  `X-Pipeline`, `X-Output-SHA256` (sha256 of the body bytes) and
  `Server-Timing` with per-node nanoseconds. The driver verifies the request
  id echo and the body hash; the correctness gate requires both. Timings are
  diagnostic only, because RocketRide exposes no equivalent.
- **One uvicorn worker** is the deployment shape. Parallelism comes from the
  graph executor and from torch releasing the GIL during inference.
- **Forbidden on purpose:** LangGraph Agent Server, `langgraph-cli`,
  `langgraph-sdk`, LangSmith, Postgres, Redis, queues. The arm is the OSS
  library in a plain service, which is the production-realistic shape being
  compared.

Run it locally for a correctness check (emulated on a Mac, never for timing):

```bash
cd aws_pdf && docker compose up -d --build tika langgraph
curl -s localhost:8100/health/ready
curl -s localhost:8100/meta | python3 -m json.tool
curl -s -i -F "file=@arms/rocketride/data/probe/sample.pdf;type=application/pdf" localhost:8100/v1/process/document-pdf-v1 | head -20
```

## 4. The video service in detail (`aws_video/arms/langgraph/`)

```
service.py   FastAPI shell: POST /process (multipart video -> documents JSON),
             GET /health/ready (models loaded AND one synthetic frame through the
             whole graph), GET /meta (pid, port, torch threads, env, checkpoint hashes)
graph.py     the five-node StateGraph over VideoState; each node records its wall time
serve8.py    matched posture: N independent single-worker uvicorn processes, one port each
workload/    frames.py detect.py chunk.py embed.py
smoke_lg.py  local one-video smoke
```

Behaviour to know:

- The upload is streamed to a temp file in 4 MB chunks, the graph runs in a
  worker thread (`anyio.to_thread`), the temp file is deleted in `finally`.
  No scratch accumulates (contrast: the RocketRide engine keeps every upload
  until its container is removed).
- Frames are written to disk and streamed into detection one at a time.
  Decoding every frame up front held 2.3 GB per 90-minute 1080p film and
  OOM-killed the service with 32 in flight (the films500 post-mortem).
- Model singletons are shared inside a process; the lock in `detect.py`
  guards loading only, unless `LG_DETECT_CONCURRENCY_PER_PROCESS` is set,
  which wraps only the `predict` call in a semaphore (the matched posture's
  mirror of the engine's per-task detector lock).
- `LG_TORCH_THREADS` and `LG_TORCH_INTEROP` are applied in lifespan before
  the models load; `/meta` reads them back so the driver can refuse to run
  against a misconfigured worker.
- The response carries `n_frames`, `n_chunks`, `total_chars`, `output_sha256`,
  per-node timings, `worker_pid` and `port` for per-request attribution.

## 5. Postures of the video LangGraph arm

| posture | compose service | how to start | client |
|---|---|---|---|
| `lg_native_1process` | `langgraph` | `uvicorn service:app` on 8200, torch default threads, no inference lock | `bench/lg_driver.py`, modes `seq` / `c<N>` |
| `lg_matched_8x4_c32` | `langgraph-matched` | `serve8.py`: 8 uvicorn processes on 8201-8208, torch 4/1, one predict at a time per worker | `bench/lg_driver_matched.py`: c32 = 8 endpoints x 4, explicit executor, barrier |
| `lg_workers8_1port_c32` | `langgraph-workers8` | `uvicorn --workers 8` on 8200, same per-worker settings, kernel-balanced | `bench/lg_driver_1port.py` |
| `lg_inproc_6x5_c24` | `langgraph-inproc` | one uvicorn, torch 5/1, inference semaphore 6, one model copy | `bench/lg_driver_matched.py` with `LG_WORKERS=1` |

The RocketRide side of each posture is described in `aws_video/README.md`
and `aws_video/MATCHED_POSTURE.md`.

## 6. Adding a new pipeline to the PDF service

1. Write the workload functions under `workload/<domain>/` with no fastapi or
   langgraph imports.
2. Write `pipelines/<name>/{state.py,nodes.py,graph.py,adapter.py}`. The
   adapter implements the Pipeline protocol; `prepare_input` validates the
   request (media type, presence of a file) and builds the initial state.
3. Register a builder in `service/app.py::default_builders()` and list the
   name in `PIPELINES`.
4. Add the corresponding `.pipe` for RocketRide and extend the drivers'
   record schema only if the output shape changes. The metrics library reads
   records, never services.

## 7. What is deliberately asymmetric, and disclosed

- RocketRide takes a whole batch in one `send_files` call and schedules it
  internally; LangGraph has no batch endpoint, so a "batch" is B concurrent
  HTTP requests joined at a barrier. The `b<B>` modes share the shape of the
  offered load, not the wire protocol. Say so whenever quoting them.
- LangGraph's per-node timings exist; RocketRide's require the engine's own
  `apaevt_flow` traces (`RR_TRACE`), which perturb the run and need a control
  cell.
- The engine keeps uploads in scratch; the LangGraph service deletes them.
  That is a reported metric (storage amplification), not something the
  harness works around.
