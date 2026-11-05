# RESULTS_AMI_POSTURES.md — RocketRide vs LangGraph on AMI, by posture

Three measured cells on the **same 168 AMI meetings** (identical bytes, identical
frames), one per process topology. Evidence grade for all three: **SIZING** —
single repetition, unpinned CPU envelope, not the publishable comparison.

| cell | run | status |
|---|---|---|
| RocketRide **default** (`rr_default_1token`) | `native170-20260822T070136Z/rr` (Run C) | measured |
| LangGraph **default** (`lg_native_1process`) | `native170-20260822T070136Z/lg` (Run C) | measured |
| RocketRide **matched 8×4** (`rr_matched_8x4`) | `matched8x4-ami-rep1-20260826T064153Z/rr` | measured 2026-08-26 |
| LangGraph **matched 8×4** (`lg_matched_8x4_c32`, 8 ports) | — | built + smoke-passed, **not yet run** |
| LangGraph **one-port 8 workers** (`lg_workers8_1port_c32`) | `matched8x4-ami-rep1-20260826T075823Z/lg` | measured 2026-08-26 (paired with RR matched) |

Raw records for every measured cell are committed under `runs/ami-postures/`
(per-doc records, run manifests, cgroup samplers, progress, provenance, census,
reports) and mirrored in `s3://rocketride-benchmark-data/leela/videobench/<run>/`.
Every number below is re-derivable: `python3 bench/report.py --arms <rr_dir> <lg_dir>`.

---

## 1. What is common to every cell

| dimension | value |
|---|---|
| box | AWS `i-0bdc8b1e18f2a5348`, c7i.8xlarge — 32 vCPU, 61 GB, 1 TB gp3; one arm at a time, other arm stopped |
| corpus | AMI `ami_full` pin, sorted-ID committed order; **first 168 measured, last 2 (TS3012c, TS3012d) as warm fixtures**; sha-verified against the corpus manifest before each run (`corpus_pin` fail-closed) |
| pipeline (both arms) | frames every 15 s (ffmpeg `fps=1/15`) → RF-DETR base, threshold 0.3 → RecursiveCharacterTextSplitter 4000/0 → `multi-qa-MiniLM-L6-cos-v1` (384-d) |
| engine | RocketRide 3.3.1 with the two documented patches (onnxruntime boot fix; chunk-duplication correction); SDK `rocketride` 1.3.0 |
| LangGraph arm | FastAPI + compiled StateGraph (frames→detect→chunk→embed→assemble); same detector checkpoint; frames streamed from disk (post-films500 fix) |
| warm-up | excluded from every metric (driver markers window the cgroup counters) |
| measurement | engine cgroup `cpu.stat`/`memory.*` sampled every 15 s; footage denominator = ffmpeg-probed video-stream duration (Run C predates the probe field → source metadata, 98.19 h vs 96.06 h probed, a 2.2% denominator difference affecting only "per footage" rows; **frames identical across cells: 137.2/video, 23,049 total**) |
| work parity (Run C, cross-arm) | input_identity 168/168 · **frame_parity exact 168/168** · detection_ratio in band · chunk_ratio in band · workload_ratio 1.024 |

## 2. Setup of each measured run

### 2.1 RocketRide default — `rr_default_1token` (Run C, 2026-08-22)
- 1 engine container, **one `use()`** with `use_existing=True` → one task process, one RF-DETR, one detector lock
- `threads=` unset (engine default 64 in-flight items), ttl 93,600 s, six BLAS/OMP vars **unset** (in-process torch = 16)
- ingestion: **blast** — one `send_files` batch of all 168 files (1 MiB chunked); 2 warm docs from the list tail
- no memory limit; all 32 vCPU; driver `bench/bench_video.py`; runner `run/native170.sh`

### 2.2 LangGraph default — `lg_native_1process` (Run C, 2026-08-22)
- 1 container, **one uvicorn process**, one shared RF-DETR + MiniLM singleton, **no inference lock** (up to 32 concurrent `predict` calls; torch releases the GIL)
- six BLAS/OMP vars unset (torch default intra-op); client **c32** (32 requests in flight, whole backlog queued); 2 warm docs
- driver `bench/lg_driver.py`; runner `run/native170.sh`
- note: this run used the pre-streaming frame buffering (all frames of a video in RAM); safe at AMI durations, fatal at films500 scale — fixed since (commit `2d7533b`), work unchanged

### 2.3 RocketRide matched — `rr_matched_8x4` (2026-08-26)
- 1 engine container `videobench-rocketride-m8`, `mem_limit 58g`, six BLAS/OMP vars = **4**
- **8 tasks**: 8 pipe copies with distinct `project_id` (`videobench-matched8x4-00…07`), **one `use()` per copy on its own client**, no `use_existing`, no `threads=`, **ttl=0** (= no expiry, verified in engine source)
- **fail-closed census per `use()`**: runner snapshots the container's process table; exactly one new task process (`engine … node.py /tmp/task-*.json`) per call, its `/proc/<pid>/environ` carrying all six vars at 4 — PASS 8/8 (pids recorded in `task_census.json`); torch inter-op inside engine tasks: engine default, unverifiable (disclosed)
- warm-up: **both fixtures to every task** (max 250 s), then a 30 s idle window
- ingestion: **sharded blast** — `shard = manifest_index % 8` (21 videos per task), 8 concurrent `send_files` released at a barrier; **admission unbounded** (one batch per shard → the engine admits up to `threads=64` per task: 114 concurrent ffmpeg decoders observed, memory pinned at the 58 GB limit). The bounded variant (`RR_INFLIGHT_PER_TASK=4`) is committed but not yet measured.
- driver `bench/bench_video_matched.py`; runner `run/matched8x4_ami.sh` (`ARM_ORDER=rr`); commits `47fe308`…`8e63ecd`
- known tax: the runner's keepalive loop burns ~1 core during the measured span (to be removed from spans in rep 2+)

### 2.4 LangGraph matched — `lg_matched_8x4_c32` (built, smoke-passed, pending)
- 1 container `videobench-langgraph-m8`, `mem_limit 58g`, six vars = 4; **8 single-worker uvicorn processes** (ports 8201–8208, `serve8.py` supervisor), each with its own model copies; `torch.set_num_threads(4)`, `set_num_interop_threads(1)` before model load; **one `predict` at a time per worker** (`LG_DETECT_CONCURRENCY_PER_PROCESS=1`, lock around the model call only)
- readback gate before warm-up: 8 distinct pids, torch 4/1, env, models loaded, identical checkpoint hash
- client **c32 = 8 endpoints × 4 active**, explicit 32-thread executor, `endpoint = index % 8`, barrier; both fixtures to every worker
- pairs with the RR matched directory via `PAIR_RR`

### 2.5 LangGraph one-port 8 workers — `lg_workers8_1port_c32` (2026-08-26)
- The "obvious" deployment: 1 container `videobench-langgraph-w8`, **`uvicorn --workers 8` on ONE port (8200)** — eight worker processes share the socket, the **kernel** picks the worker at `accept()`; the client cannot address, warm or verify a specific worker
- per-worker settings identical to the matched cell (six vars = 4, torch 4/1, one `predict` at a time per worker, 58 GB) — the only difference from `lg_matched_8x4_c32` is port layout / load balancing
- verification as far as one port allows: `/meta` polled until 8 distinct pids answered (pids 8–15), each validated (torch 4/1, env, models, identical checkpoint); warm-up re-sent the two fixtures until every pid had served (32 warm requests, 2 rounds); 30 s idle window
- client c32 global (explicit 32-thread executor, barrier); serving worker pid recorded per video
- driver `bench/lg_driver_1port.py`; runner `run/matched8x4_ami.sh` (`ARM_ORDER=lg1`); commit `6d9ad0d`

**Result (168/168, all gates PASS, workload_ratio vs RR matched 1.024):** span **3,434 s (57.2 min)**, **6.71 f/s / 100.7×**, effective cores **11.26 (35%)**, CPU-s/frame **1.68** (the lowest of any cell — little contention), CPU-s/footage-min 6.71, peak memory 24.9 GB, idle burden 1.1 cores, $14.18 / 1k footage-h, latency p50 233 s · p95 1,582 s · p99 2,174 s.
**Kernel load skew reproduced — videos per worker pid: 49 / 43 / 29 / 15 / 14 / 11 / 4 / 3** (even share = 21; Ansh's WS-1 saw 48/29/28/25/19/7/7/5). The client held 32 requests in flight throughout (observed max 32), but with one inference at a time per worker and the kernel piling connections onto a few workers, on average only ~5 of 8 workers were busy — two-thirds of the box idle while a queue formed on the hot workers. This is a property of the serving stack, not of LangGraph's processing speed; the deterministic 8-port cell exists precisely to remove it.

### 2.6 RocketRide matched — `rr_matched_6x4` with `threads=32` per connection (2026-08-26, run `matched6x4-ami-rep1-20260826T155110Z/rr`)
- identical to §2.3 except: **6 tasks** (6 connections, `project_id` `videobench-matched6x4-00…05`, 28 videos per shard) and **`use(threads=32)` on every connection** — the first cell with the engine's per-connection admission cap set explicitly (engine team, rocketride-server#2053: in-flight width = connections × threads). Engine-side proof: each task's `/tmp/task-*.json` carries `"threadCount": 32`; provenance `threads_arg: 32`, `engine_item_threads_effective: 32`, `admission_width_box: 192`.
- 192 admission slots ≥ 168 videos → the whole corpus was admitted at the barrier (threads non-binding at 28 videos/shard); one native batch per shard (`RR_INFLIGHT_PER_TASK=0`), BLAS/OMP = 4, `ttl=0`, `mem_limit 58g`. task_census PASS 6/6.
- driver knob added this run: `RR_THREADS_PER_CONN` (`bench/bench_video_matched.py`, commits `8fb65e2`, `d23edf0`); runner honors `RR_TASKS` overrides.

## 3. Results — every metric, side by side

| metric | RR default | LG default | **RR matched 8×4** | LG one-port 8 workers | LG matched 8×4 (8 ports) |
|---|---|---|---|---|---|
| gates | all PASS | all PASS | all PASS + task_census 8/8 | all PASS | pending |
| span | 9,445 s (2.62 h) | 2,271 s (37.9 min) | **2,082 s (34.7 min)** | 3,434 s (57.2 min) | — |
| **frames/s** | 2.44 | 10.15 | **11.07** | 6.71 | — |
| × realtime | 37.4× | 155.7× | 166.1× | 100.7× | — |
| videos/s | 0.018 | 0.074 | 0.081 | 0.049 | — |
| chunks/s | 1.52 | 6.18 | 6.89 | 4.08 | — |
| chunks / video | 85.5 | 83.5 | 85.4 | 83.5 | — |
| effective cores | 5.98 | 26.84 | 23.87 | 11.26 | — |
| scaling efficiency (of 32) | 18.7% | 83.9% | 74.6% | 35.2% | — |
| threads activated | 4,049 | 2,932 | 13,513 | 1,311 | — |
| **CPU-s / frame** | 2.53 | 2.73 | **2.16** | **1.68** | — |
| CPU-s / footage-min | 9.91 | 10.66 | 8.62 | 6.71 | — |
| CPU-s / video | 347 | 374 | 296 | 230 | — |
| CPU-s / detection | 0.296 | 0.320 | 0.252 | 0.197 | — |
| CPU-s / chunk | 4.06 | 4.48 | 3.46 | 2.76 | — |
| peak memory (cgroup, incl. cache) | 42.9 GB | 28.8 GB | 57.4 GB (at limit) | 24.9 GB | — |
| cold-to-ready | 138 s | 72 s | warm 250 s per task | warm 2 rounds | — |
| idle burden (cores, no work) | n/a | n/a | 3.9 | 1.1 | — |
| driver CPU (separate) | n/a | n/a | 0.02 cores | 0.05 cores | — |
| stage split | black box | frames 7% · detect 92% · embed 1% | black box | frames 2% · detect 98% · embed 1% | — |
| latency basis | batch: TTFR 260 s; completion p50 5,565 s | per-request: p50 430 s · p95 708 s · p99 787 s | batch: TTFR 400 s; completion p50 1,992 s | per-request: p50 233 s · p95 1,582 s · p99 2,174 s | — |
| **$ / 1k footage-hours** ($1.428/h) | $38.15 | $9.17 | **$8.60** | $14.18 | — |
| videos/day at 35-min mean | 1,537 | 6,393 | 6,972 | 4,224 | — |
| load distribution | 1 task | 1 process | 21 videos per task (dealt) | **49/43/29/15/14/11/4/3 per worker (kernel)** | — |
| evidence grade | SIZING | SIZING | SIZING | SIZING | — |

Latency rows are never compared across arms: batch completion and HTTP request latency are different semantics.


### 3.1 RocketRide matched: 8 tasks / threads default 64 vs 6 tasks / threads 32 (same corpus, same day)

| metric | `rr_matched_8x4` (8 conn × 64 = 512 slots) | **`rr_matched_6x4` threads=32 (6 conn × 32 = 192 slots)** |
|---|---|---|
| gates | all PASS + task_census 8/8 | all PASS + task_census 6/6 |
| span | 2,082 s (34.7 min) | **1,951 s (32.5 min)** |
| frames/s · × realtime | 11.07 · 166.1× | **11.81 · 177.2×** |
| videos/s · chunks/s | 0.081 · 6.89 | 0.086 · 7.36 |
| chunks / video · frames / video | 85.4 · 137.2 | 85.4 · 137.2 (identical work) |
| effective cores (of 32) · scaling eff. | 23.87 · 74.6% | 21.39 · 66.8% |
| threads activated | 13,513 | 13,653 |
| CPU-s / frame · / footage-min · / video | 2.16 · 8.62 · 296 | **1.81 · 7.24 · 248** |
| CPU-s / detection · / chunk | 0.252 · 3.46 | 0.212 · 2.91 |
| peak memory (cgroup, incl. cache) | 57.4 GB (at limit) | 57.2 GB (at limit) |
| idle burden · driver CPU | 3.9 · 0.02 cores | 4.33 · 0.02 cores |
| batch TTFR · completion p50 / p90 / last | 400 s · 1,992 / — / — | 318 s · 1,877 / 2,182 / 2,319 s |
| $ / 1k footage-hours | $8.60 | **$8.06** |
| evidence grade | SIZING | SIZING (single rep) |

Reading: fewer tasks with an explicit cap did the identical work 6% faster on ~2.5 fewer cores — CPU per frame down 16%. Both cells admitted the whole corpus (512 and 192 slots vs 168 videos), so this is not yet a width-limited cell; memory sat at the 58 GB limit in both. Next cells to isolate width: `threads=16` (96 slots) or `RR_INFLIGHT_PER_TASK=4`.

## 4. Findings

1. **Tokens are RocketRide's throughput lever.** Same engine, same corpus: 1 task → 2.44 f/s at 6 cores; 8 tasks → 11.07 f/s at 24 cores (**4.5×**). The single-task sweep of `threads=` (8/32/64) was flat because that argument bounds in-flight items, not parallelism; the per-task ceiling is the detect node's device lock (`nodes/detect/IGlobal.py:81`) plus the task's single event loop. The retracted earlier wording "architectural ceiling" is replaced by **"per-task ceiling; multi-task escapes it."** Reproduces Ansh's WS-1 equal-config cell within 8% (12.05 f/s).
2. **Out of the box, LangGraph is 4.2× faster and cheaper** (default vs default) — a topology gap, not a framework gap.
3. **With tokens, RocketRide edges LangGraph-default on throughput and is more CPU-efficient per frame** (2.16 vs 2.73 CPU-s/frame), at 2× the memory and a 3.9-core idle burden. The fair statement needs the LangGraph matched cell (pending).
4. **Admission is a real dimension for RocketRide.** A whole-shard batch lets the engine run ~21 decoders per task (114 total), pinning memory at the limit; it cost ~8% vs Ansh's client-bounded feed. Now an explicit, recorded setting.
5. **Per-core efficiency vs wall-clock split** is expected to persist in the matched pair; both facts belong in any summary.
6. Mid-run rate estimates on a blast are unreliable (long meetings lead the committed order); only the completed span is quoted.

## 5. Remaining plan

- LangGraph matched 8×4 (option A), paired: ~1 h → completes the fair row.
- Optional cells: RR bounded admission (`RR_INFLIGHT_PER_TASK=4`, ~40 min); LG 8 workers without the lock (variant B); each arm at its own best (RR 16×2 or bounded 8×4; LG single-process with torch pinned to 1–2 threads per call).
- Publishable campaign: cpuset envelope (driver/samplers/keepalive outside it), ≥3 reps with alternating arm order, medians with spread, determinism exercised.

## 6. Pointers

- Posture spec and every config change: `MATCHED_POSTURE.md`
- Metric definitions: `METRICS.md`, `METRICS_EXPLAINED.md`; default-row narrative: `RESULTS_AMI_FULL.md`, `RESULTS.md`
- Raw data: `runs/ami-postures/` (this doc) · `runs/films500-sizing/` (films corpus) · `runs/rrsweep-20260823/` (thread sweep)
