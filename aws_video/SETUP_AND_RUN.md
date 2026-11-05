# Video Benchmark — Setup & Run Reference (team status, 2026-08-21)

The complete operational picture: infrastructure, both services, the
pipeline configuration, how a run executes (no waves — see why), and the
current state of every asset. Enough detail to operate the benchmark
from a fresh clone + AWS access.

## 1. Infrastructure

| item | value |
|---|---|
| instance | `i-0bdc8b1e18f2a5348`, **c7i.8xlarge** (32 vCPU / 61 GB RAM), us-east-1a |
| disk | **1 TB gp3** (resized from 100 GB on 2026-08-21; ~900 GB free) |
| RAM tmpfs | `/dev/shm` ≈ 30 GB (used by the wave runner; idle otherwise) |
| access | SSM sessions only (no SSH/scp); laptop control via `local/box.sh` (start/stop/run/launch/tail); sessions need a pty (agent ≥3.3.4793.0 — box.sh handles it) |
| auth | laptop: SSO profile `leela` (expires every few hours, browser re-login); box: instance role (never expires; long runs unaffected) |
| S3 bucket | `s3://rocketride-benchmark-data/leela/` — box role has put+get; laptop has get |
| S3 layout | `corpus/<set>/` (videos + corpus_manifest.json), `videobench/<run>-<stamp>/` (all results, live-synced every 60 s during runs) |
| cost | ~$1.43/h while running; **always `box.sh stop` after work** (idle auto-stop exists but is not guaranteed prompt) |

## 2. The two arms (one engine each; only the framework varies)

**RocketRide** — `arms/rocketride/Dockerfile` → image
`videobench-rocketride:3.3.1`, container `videobench-rocketride`,
websocket port 5565.
- Engine release 3.3.1, tarball SHA-pinned (build fails on a silent swap).
- Two documented patches, recorded in image labels + provenance:
  (1) onnxruntime-gpu 1.20.1→1.20.2 pin fix — 3.3.1 cannot boot on Linux
  without it, and the broken pin sits in the video nodes' own
  requirements; (2) `BUG_CHUNK_DUPLICATION` — `preventDefault()` after
  the embedding flush, without which ~40% of results are emitted twice
  (measured on 987 PDFs; gate `self_duplication` guards it forever).
  Describe results as "3.3.1 with the documented duplication correction",
  never "stock".
- Driven by `bench/bench_video.py` via the `rocketride==1.3.0` SDK:
  modes blast (one `send_files` batch — its native path), seq, c\<N\>.
- Behavior to know: stores a copy of EVERY uploaded video in container
  scratch, released ONLY by container removal (survives batch end and
  `terminate()`); `use(threads=N)` is recorded but proven a no-op for
  video utilization.

**LangGraph** — `arms/langgraph/` → image `videobench-langgraph:v1`,
container `videobench-langgraph`, HTTP port 8200. Full architecture:

*Three-layer design* (same layering as the PDF arm, so the framework
only orchestrates and the computation is framework-free):

```
┌──────────────────────────────────────────────────────────────────┐
│ service.py — FastAPI transport shell                             │
│   POST /process · GET /health/ready · GET /meta                  │
│   (maps to RocketRide's webhook + response_documents — transport │
│    is the measurement boundary, identical on both arms)          │
├──────────────────────────────────────────────────────────────────┤
│ graph.py — LangGraph StateGraph (the orchestration under test)   │
│   START → frames → detect → chunk → embed → assemble → END       │
├──────────────────────────────────────────────────────────────────┤
│ workload/ — pure computation, no fastapi/langgraph imports       │
│   frames.py · detect.py · chunk.py · embed.py                    │
└──────────────────────────────────────────────────────────────────┘
```

*The graph*: a linear five-node `StateGraph` over a `VideoState`
TypedDict (`video_path, frames, det_lines, chunks, embeddings,
documents, timings`), compiled ONCE at startup, no checkpointer
(stateless request/response — persistence would be dead framework
weight). Each node returns a partial state update and appends its wall
time to `timings` — the source of the per-node decomposition in reports
(decode ~55%, detect ~40%, embed ~5% on the box), which the closed
engine cannot provide. The ~30 MB frame list is dropped from state
right after detection to keep concurrent-request memory flat. Node names
mirror the RR pipe 1:1 (`frames`~frame_grabber_1, `detect`~detect_1,
`chunk`~preprocessor_1, `embed`~embedding_1, `assemble`~response_1);
RR's transport components are deliberately NOT graph nodes — matched
measurement boundaries, not matched internal topology.

*Node implementations* (`workload/`):
- `frames.py` — `ffmpeg -vf fps=1/15` into a TemporaryDirectory as
  **lossless PNG** (JPEG would perturb the detector's pixels), then PIL
  RGB. The fps filter matches the engine's own reader semantics — frame
  counts proven identical (28/28 frame-parity, 102=102 on ES2016d).
  Binary: system ffmpeg, else pip `imageio-ffmpeg` (the engine's own
  fallback trick).
- `detect.py` — `rfdetr` `RFDETRBase` (the backend the engine's
  detection module prefers), threshold 0.3. Lazy singleton behind a
  `threading.Lock` that guards model LOADING only — inference is not
  serialized. COCO names from `rfdetr.assets.coco_classes` (≥1.9
  layout) with a legacy fallback. Output: one JSON line per frame in
  the engine's exact format (`json.dumps` defaults — verified byte-what
  the engine emits).
- `chunk.py` — `RecursiveCharacterTextSplitter(4000, 0)` over
  newline-joined lines; parameters recovered empirically (4000/0
  reproduces the engine's chunks byte-exactly; 4096 and 3600 do not).
  Dense rooms exceed 4000 chars per frame-line → mid-line splits →
  chunks are text windows, not valid JSON, on BOTH arms (faithful
  replication includes the quirk).
- `embed.py` — `SentenceTransformer` **multi-qa-MiniLM-L6-cos-v1**,
  CPU, normalized, lazy singleton — the model the engine's `miniLM`
  profile actually resolves to (verified to 1.06e-07).

*Service shell*: lifespan builds the graph then runs a warmup (a
synthetic 352×288 frame through detect→chunk→embed) BEFORE
`/health/ready` returns 200 — cold model loads can never land inside a
measured request; drivers gate on readiness. `POST /process` streams
the upload to a NamedTemporaryFile in 4 MB chunks, runs the graph via
`anyio.to_thread` (event loop stays live for health checks), returns
documents + `n_frames`/`n_chunks`/`total_chars`/`output_sha256` +
per-node timings, then deletes the temp file — **no scratch
accumulation** (contrast RR's retained uploads). `GET /meta` returns
the arm's config identity for provenance.

*Concurrency model — why c6 reached ~19 cores*: one uvicorn process;
each in-flight request runs one graph invocation in a worker thread;
model singletons are SHARED (anon RSS flat ~2.8 GB at any concurrency).
Parallelism comes from torch releasing the GIL during inference (six
threads = six genuine parallel inferences, each spread further by
torch's intra-op threading when unpinned) and ffmpeg running as
separate subprocesses. Measured: 19.5 effective cores from 225 OS
threads vs the engine's 5.45 cores from ~1,000 threads — same per-unit
CPU cost, ~3.6× the parallelism. Under the matched envelope
(`OMP_NUM_THREADS=1` both arms) torch's intra-op spread is removed and
the c\<N\> window remains the variable under test.

*Determinism (measured)*: same video on the same host → byte-identical
output (rep pairs: identical output_sha256 and embedding digests);
across hosts ±1% (ffmpeg builds / torch numerics flip borderline
detections) — hence all gates compare within-platform and arm-vs-arm
runs are box-vs-box only.

*Driven by* `bench/lg_driver.py` (same `per_doc.jsonl` schema as the RR
driver): modes seq, c\<N\> — its native ingestion is per-request HTTP;
there is no batch API, and that asymmetry is documented rather than
papered over. Versions float for smokes; **`pip freeze` pin before
measured runs** (the rfdetr-1.9 import move is the cautionary tale).

Shared: docker volume `rr-model-cache` (~9.4 GB: torch stacks, rfdetr
checkpoint, MiniLM weights) mounted by BOTH arms — models download once
per box, ever. **Critical model fact:** RocketRide's `miniLM` profile =
`sentence-transformers/multi-qa-MiniLM-L6-cos-v1` (NOT all-MiniLM-L6-v2,
which is its `miniAll`); wrong model passes every structural check and
is silently incomparable (verified to 1.06e-07 against engine vectors).

**Client** — `smoke/smoke.Dockerfile` → `videobench-smoke` (python
3.12-slim + rocketride SDK + requests). Runs the drivers in its OWN
container so client CPU/RSS never lands in the measured arm's cgroup and
every video crosses a real network hop. Mounts: corpus read-only,
`./results`, `./pipe`, `./bench`.

## 3. The pipeline contract

`pipe/benchmark_video_detect.pipe` (RocketRide executes this; LangGraph
mirrors it stage-for-stage — parameters recovered byte-exactly from
engine output):

| stage | RR component | config |
|---|---|---|
| ingest | webhook (video lane) | file upload via SDK / HTTP |
| frames | frame_grabber | interval profile, **15 s** (≈121 frames per 30-min video) |
| detect | detect | **rfdetr**, threshold **0.3**, output: one JSON array per frame `[{"label","score","box":{x1,y1,x2,y2},"centroid":{x,y}}, …]` |
| chunk | preprocessor_langchain | RecursiveCharacterTextSplitter, **4000 chars, 0 overlap** (frame lines joined by \n; dense rooms >4000 chars/line split mid-line — chunks are text windows, not valid JSON; both arms identical) |
| embed | embedding_transformer `miniLM` | multi-qa-MiniLM-L6-cos-v1, **384-dim**, L2-normalized |
| respond | response_documents | chunks + vectors back to client |

`pipe/benchmark_video.pipe` is the dual-lane variant (+ faster-whisper
transcription); it carries the known Whisper nondeterminism (R5) and is
not the current benchmark pipe.

### 3.1 The object-detection stage, in both arms (the pipeline's cost center)

Both arms run **the same detector: RF-DETR** (Roboflow's DETR-family
real-time detector, Apache-2.0, "base" variant — ~29M params, 560-px
input, COCO-80 classes), on CPU, threshold 0.3. The sameness is by
construction; the differences are in hosting:

| | RocketRide | LangGraph |
|---|---|---|
| component | `detect` node, profile `rfdetr` | `detect` graph node → `workload/detect.py` |
| implementation | engine's `ai/common/models/vision/detection.py`, which PREFERS the same `rfdetr` pip package (`RFDETRBase`); RT-DETR only as fallback | `rfdetr` pip package, `RFDETRBase`, directly |
| package version | whatever the engine resolved at build (older API generation) | current release (1.9.x — note the `assets.coco_classes` relocation) |
| weights | `rf-detr-base.pth`, auto-downloaded to the engine model cache | same checkpoint family, in the shared `rr-model-cache` volume |
| model instances | ONE shared instance behind a device lock — prime suspect for the ~6-core ceiling | one shared instance; lock guards LOADING only, inferences run concurrently (torch releases the GIL) |
| output | one JSON array per frame: `[{"label","score","box":{x1,y1,x2,y2},"centroid":{x,y}}, …]` | byte-identical format (replicated from a captured engine response) |
| measured cost | ~2.62 cpu-s/frame | ~2.36–2.74 cpu-s/frame — statistically the same work |

Notes:
- The engine also ships an open-vocabulary profile (`mmgdino`,
  Grounding-DINO); both arms deliberately use closed-set `rfdetr` —
  deterministic, local, pip-replicable.
- The package-version gap is the ONLY real difference and it is
  measured: a handful of borderline detections flip, keeping per-video
  detection counts within 2.4% (e.g. 332 vs 328 on ES2016d). This is
  why the cross-arm gate is a ±10% band, not byte parity (decision
  2026-08-20); chunk-count drift (±3) and workload ratio (1.013)
  inherit from it.
- On AMI footage it detects meeting-room reality — person, chair, tv,
  laptop, cell phone, books: ~3 detections/frame in Edinburgh rooms,
  ~16/frame in Idiap rooms (bookshelves) — the source of the 5×
  chunk-density variance the work-normalized metrics absorb.
- It is the pipeline's true cost center: ~87–92% of LangGraph's time
  under concurrency (decode parallelizes away), and the per-frame CPU
  numbers above are where the two arms prove equally efficient — the
  head-to-head gap comes from how many inferences run AT ONCE, not how
  fast each one is.

## 4. Corpus

| set | contents | where |
|---|---|---|
| `ami30test` | 30 muxed videos (3.6 GB), 23–37 min each | S3 + cached on box at `~/bench_corpus_ami30test` |
| `ami_full` (staging pending) | all 170 usable AMI meetings, 99.5 h, ~24 GB | list: `corpus/sets/ami_full.txt`; durations: `corpus/sets/ami_full_durations.json`; stager: `run/stage_corpus.sh` (mirror→mux→S3, delete-as-you-go, resumable, ~1.5–2 h one-time) |

Every corpus doc is one muxed file (AMI ships video and audio
separately; `Closeup1.avi` + `Mix-Headset.wav` → ffmpeg stream-copy,
bitexact). Manifests carry per-video `duration_s`; staged sets add
per-file sha256 (feeds the `corpus_pin` gate). **Known metadata issue**
(found by `frame_law`, 2026-08-21): some AMI recordings' video stream is
longer/shorter than the audio (ES2008c, ES2011c) — staging will add
ffprobe VIDEO durations to fix the expectation.

## 5. How a run executes (current shape — no waves)

With 1 TB, the whole corpus + engine scratch is <5% of disk, so each run
is one straight pass. Reference: `run/headtohead.sh` (the shape of the
2026-08-21 run):

```
[1] corpus    S3 → ~/bench_corpus_ami30test if not cached (~25 s)
[2] build     docker compose build rocketride langgraph smoke (cached: ~1 min)
[3] ARM 1     compose up rocketride → healthcheck (port 5565)
              → sampler starts: every 15 s, cgroup cpu_usage_usec,
                memory.current, pids.current, memory.stat anon
                → results/<run>/rr/engine_cgroup.csv
              → client: bench_video.py /corpus rr-outdir N MODE WARM
                (h2h: N=28, MODE=c6, WARM=2 — warm videos excluded)
              → per_doc.jsonl + progress.jsonl written; container logs saved
              → compose stop + rm  ← releases the engine's upload scratch
[4] ARM 2     same, with langgraph (port 8200, healthcheck /health/ready)
              and lg_driver.py — SAME videos, SAME order, SAME mode
[5] report    python3 bench/report.py --arms rr lg → report.txt
              (gates first, fail-closed; numbers stamped diagnostic on FAIL)
[6] sync      aws s3 sync → videobench/<run>/ — ALSO ran every 60 s
              throughout, so a crash loses ≤1 minute of records
```

Launch/observe/stop from the laptop:

```bash
aws sso login --profile leela                      # if token expired
bash local/box.sh start
bash local/box.sh run  'cd ~/rocketride-benchmarking && git pull --ff-only origin main'
bash local/box.sh launch h2h 'cd ~/rocketride-benchmarking/aws_video && bash run/headtohead.sh'
bash local/box.sh tail h2h               # progress
bash local/box.sh stop                   # ALWAYS
```

Knobs (env): `N` (measured videos), `WARM` (excluded warm-ups), `MODE`
(blast | seq | c\<N\>), `BENCH_TIMEOUT_S` (default 21600), `BENCH_PIPE`,
`RR_THREADS` (recorded; proven no-op for video), `CORPUS_DIR`.

Measured phase timings (h2h, 28+2 videos/arm): RR arm ≈ 26 min
(warm 118 s + span 1,433 s), LG arm ≈ 9 min (boot+warm + span 377 s),
report <5 s. Whole run ≈ 40 min including builds.

## 6. Results layout (per run, in S3 and on-box `results/`)

```
h2h-<stamp>/
  rr/  per_doc.jsonl      one record per video: ok, reason, input_sha256,
                          size, duration_s, submit/completion ns, n_chunks,
                          total_chars, n_frames_est, n_detections,
                          chunk_sha256[], embedding_sha256, vector_dim,
                          l2_norms_minmax, timing_source
       + shot_meta line:  mode, span_s, ok_docs, measured_audio_s,
                          offered_concurrency, threads_requested,
                          pipe_sha256, provenance{}, envelope, warm stats
  rr/  progress.jsonl     one line per video AS IT FINISHED
  rr/  engine_cgroup.csv  ts,cpu_usage_usec,mem_current,pids,anon_bytes
  rr/  driver.log, service.log
  lg/  (same five, from lg_driver.py — identical schema)
  report.txt              gates + V0–V5 (see METRICS_EXPLAINED.md)
```

Reanalyze anywhere, anytime:
```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/videobench/<run>/ ./run --profile leela
python3 bench/report.py --arms ./run/rr ./run/lg
```

## 7. Why no waves (and when they return)

Waves (process a slice → delete the engine container → reclaim its
scratch → next slice; `run/run_waves.sh`, S3→/dev/shm staging,
resumable) existed because RocketRide's scratch retention could fill the
old 100 GB disk mid-run — it did once (ENOSPC, 47/60 failed,
2026-08-19). At 1 TB: AMI-scale runs (24 GB corpus + 24 GB scratch) run
in one pass; waves remain the tool for corpora larger than disk (a
5,000-video campaign ≈ 700 GB scratch) and for checkpointing multi-day
campaigns (a crash costs one wave, not the campaign).

## 8. Current status & what's left before quotable results

Done: both arms built + box-verified; suite implemented + exercised
cross-arm; S3 corpus architecture validated (158 MB/s pulls); 1 TB live;
first head-to-head complete (see METRICS_EXPLAINED.md for numbers).

Outstanding:
1. **Envelope wiring** — shared BENCH_CPUSET, OMP_NUM_THREADS=1 on both
   arms, one deadline, client pinned to its own cores (port from
   aws_pdf/docker-compose.yml). This is what makes runs *matched*.
2. **≥3 reps per arm per mode** (determinism + CV gates require them).
3. **seq runs** for both arms (true service latency + the cross-mode
   speedup/parallel-efficiency block).
4. **Full-corpus staging** (`run/stage_corpus.sh`, ~2 h) with ffprobe
   video-durations added to the manifest.
5. Optional probes: two-pipelines-in-one-engine, math-library threading
   (see `findings/rocketride_cpu_utilization.md`).
