# MATCHED_POSTURE.md — `matched8x4_native`: what changed, config by config

> Eight-process/model architecture-matched comparison using native ingestion:
> RocketRide sharded blast versus LangGraph c32 HTTP saturation.
> (Not a claim that batch latency and HTTP request latency have equivalent semantics.)

Status (2026-08-25): **smoke PASSED** on AMI (`matched8x4-ami-rep1-20260825T181925Z`,
8 measured + 2 warm; every gate green on both arms incl. the new census gates).
Full 168-meeting run awaits GO. Commits: `47fe308` (posture), `510bfa5`, `a738d0f`,
`7fd5da3`, `d09f974` (fixes found by the smoke iterations).

The default posture is untouched: `bench_video.py` / `lg_driver.py` /
`films_v2.sh` / `native170.sh` and every archived result keep their identities
(`rr_default_1token`, `lg_native_1process`). The matched posture is new files and
new compose services with their own names.

---

## 1. Side-by-side: default posture vs matched posture

| dimension | default (`rr_default_1token` / `lg_native_1process`) | **matched (`rr_matched_8x4` / `lg_matched_8x4_c32`)** |
|---|---|---|
| RR model processes | 1 task (one `use()`, `use_existing=True`) | **8 tasks** — 8 pipe copies, distinct `project_id`s, one `use()` each on its own client, no `use_existing`, no `threads=` |
| RR inference per model | 1 at a time **per task process** — the detect node holds one real `threading.Lock` per task (`nodes/detect/IGlobal.py:81` `make_device_lock()`, a `threading.Lock` in local mode; `nullcontext` only with `--modelserver`, which we don't run) and runs `detector.detect(image)` under it **one frame at a time** (`nodes/detect/IInstance.py:106`). Frame decode and emit run outside the lock, concurrently across in-flight items. `threads=` is a *separate* layer — `asyncio.Semaphore(threadCount)` per connection on in-flight items (`data_conn.py:117/138`, admission), not inference. Whisper additionally has per-model locks. (Earlier wording here said "no lock / no callers" — wrong; corrected 2026-08-26.) | 1 at a time × 8 models (8 locks → up to 8 concurrent forward passes box-wide, each on 4 intra-op threads) |
| RR torch/BLAS | container env unset (in-process torch default = 16) | **six vars = 4** on the container, verified in every task's `/proc/<pid>/environ` |
| RR pipeline TTL | 93,600 s (finite, > timeout) | **`ttl=0`** = no expiry (verified in engine source) |
| RR ingestion | blast: ONE `send_files` batch | **sharded blast**: `index % 8` round-robin, 8 concurrent `send_files` released at a barrier |
| RR memory | no explicit limit | **`mem_limit: 58g`** |
| LG processes | 1 uvicorn, 1 model shared by 32 threads (no inference lock) | **8 single-worker uvicorn** on ports 8201–8208 via `serve8.py`; 1 model each |
| LG inference per model | unlimited concurrent `predict` on the shared model | **1 at a time per worker** (`LG_DETECT_CONCURRENCY_PER_PROCESS=1`, lock around `predict` only) |
| LG torch | default (intra-op = cores) | **`set_num_threads(4)`, `set_num_interop_threads(1)`** before model load; six env vars = 4 |
| LG client concurrency | c32 to one port | **c32 = 8 endpoints × 4 active**, explicit 32-thread executor, `index % 8` endpoint map, barrier |
| LG memory | `mem_limit: 52g` | **`mem_limit: 58g`** |
| warm-up | 2 in-corpus docs (list tail), one process | **2 DISJOINT fixtures in a separate `/warm` mount, sent to EVERY task / EVERY worker** |
| measured span | driver markers (warm excluded) | same, plus a **30 s idle-burden window** between warm-up and the barrier |
| verification gates | census, structure, frame_law, self_dup, corpus_pin | same **+ `task_census` (RR) / `worker_census` (LG)** — fail-closed |
| nominal math budget | — | 8 processes × 4 torch threads = 32 vCPU |

---

## 2. RocketRide arm — `bench/bench_video_matched.py` (new driver)

**Task creation (8 genuinely independent tasks).**
- 8 runtime pipe copies from `benchmark_video_detect.pipe`, each with
  `project_id = videobench-matched8x4-00 … -07` (deterministic, distinct).
- One `RocketRideClient` per task (own websocket), `use(filepath=<copy>, ttl=0)`.
  **Not passed:** `use_existing` (server would return the existing task — task
  identity is `(owner, project_id, source)`, `useExisting` short-circuits) and
  `threads=` (per-task *item* concurrency, engine default `CONST_DEFAULT_MAX_THREADS = 64`,
  not torch threads — recorded as OMITTED / expected default 64).
- `ttl=0`: engine source `task_server.py:334` "Tasks with ttl=0 have no timeout".

**Fail-closed task census (per `use()`, not once at the end).**
- Driver ↔ runner handshake over the shared results volume: driver writes
  `census/<seq>.request`, runner answers `census/<seq>.json` with the engine
  container's process table (`docker exec … /proc` scan).
- Task processes are recognized by their real signature (probed on 3.3.1):
  `/opt/rocketride/engine/engine …/ai/node.py /tmp/task-<hash>.<source>-<id>.json --autoterm …`,
  child of the `eaas.py` server. (Nothing in the container has "python" in its
  cmdline — the engine binary is `./engine`.)
- Rule: before creation, **zero** task processes may exist (a stale `ttl=0` task
  never dies on its own); after each `use()`, **exactly one** new task process,
  whose environ carries all six BLAS/OMP vars at `4`. Any miss → terminate all
  tokens, abort before warm-up.
- Recorded in `task_census.json`: declared tasks, pids before/after, new task
  pids, cmdlines, project ids, redacted token digests (sha256[:16] — never the
  token), environ readback per pid, and the disclosure that torch **inter-op**
  inside engine tasks is unsettable/unverifiable.

**Warm-up:** both fixtures to every task concurrently; each task must return
both docs or the run aborts. Warm events are cleared before measurement.

**Idle-burden window:** 30 s sleep after warm-up with epoch markers, so the
report can quote the CPU the 8 idle tasks burn with no work (5.4 cores on the smoke).

**Sharding + blast:** `shard = manifest_index % 8` in committed order (no
longest-first); 8 `send_files` coroutines gathered behind one barrier;
`measurement_start_epoch_ns` immediately before release, end after the last
result. Chunked `send_files` path as before — no whole-file messages.

**Per-doc records add:** `manifest_index`, `token_index`, `project_id`,
`task_pid`, `shard_local_index`; explicit engine errors take precedence over
"no documents".

**shot_meta adds:** `posture`, `tasks`, `task_census`, `warm_records`,
`idle_window_epoch_ns`, provenance fields `threads_arg: OMITTED`,
`engine_item_threads_expected_default: 64`, `blas_omp_threads_per_task: 4`,
`torch_interop_per_task: engine default (unverifiable)`, `ttl_semantics`.

## 3. LangGraph arm — `serve8.py`, `service.py`, `detect.py`, `lg_driver_matched.py`

**`serve8.py` (new multi-process entrypoint):** spawns `LG_WORKERS` (8)
single-worker uvicorn processes, one port each (`LG_PORT_BASE` 8201 + i), env
`LG_PORT`/`LG_WORKER_INDEX` per child; forwards SIGTERM/SIGINT; **exits nonzero
and kills the rest the moment any worker dies**; worker logs inherited. Not
`--workers 8` on one port (kernel connection balancing skews).

**`service.py` changes (env-gated; default posture unaffected):**
- `_configure_torch()` runs first in lifespan, before the graph/models load:
  `torch.set_num_interop_threads(LG_TORCH_INTEROP)` then
  `torch.set_num_threads(LG_TORCH_THREADS)` (inter-op must precede any parallel work).
- `/meta` now reads back: `pid`, `port`, `worker_index`, `graph_compiled`,
  `rfdetr_loaded`, `minilm_loaded`, `rfdetr_checkpoint_sha256`,
  `minilm_checkpoint_sha256`, `torch.{num_threads,num_interop_threads,version}`,
  the six env vars + `MALLOC_ARENA_MAX`, `detect_concurrency_per_process`.
- `/process` responses carry `worker_pid` and `port` (per-request attribution).

**`workload/detect.py`:** `LG_DETECT_CONCURRENCY_PER_PROCESS` (0/unset = native
unlocked posture, unchanged). When 1: a per-process semaphore around
`model.predict` **only** — extraction, chunking, embedding, graph execution and
the HTTP request still overlap.

**`lg_driver_matched.py` (new driver):**
- Waits for all 8 `/health/ready`, then a **readback gate**: 8 distinct pids,
  torch == 4/1, six env vars == 4, models loaded, detect concurrency == 1,
  **identical RF-DETR checkpoint hash across workers** — else abort.
- Warm-up: both fixtures to every endpoint (endpoints in parallel), must
  succeed on all 8 distinct worker pids.
- 30 s idle window with markers.
- c32: `ThreadPoolExecutor(max_workers=32)` (explicit, not the default executor),
  per-endpoint `Semaphore(4)`, `endpoint = manifest_index % 8`, all requests
  created then released at a barrier; records `observed_max_global_requests`
  and `observed_max_requests_per_endpoint`.
- Uploads stream via `requests-toolbelt` (post-mortem fix carried over).

## 4. Compose — `docker-compose.yml`

New services (via `extends`, compose ≥ 2.20; the box runs v5.4):
- `rocketride-matched` → `videobench-rocketride-m8`, `mem_limit: 58g`,
  `OMP/MKL/OPENBLAS/VECLIB/NUMEXPR/TORCH_NUM_THREADS=4`.
- `langgraph-matched` → `videobench-langgraph-m8`, `command: python serve8.py`,
  `mem_limit: 58g`, the six vars = 4, `LG_TORCH_THREADS=4`, `LG_TORCH_INTEROP=1`,
  `LG_DETECT_CONCURRENCY_PER_PROCESS=1`, `LG_WORKERS=8`, `LG_PORT_BASE=8201`,
  `MALLOC_ARENA_MAX=2`; healthcheck polls **all eight** ports.
- `smoke` (client): `ROCKETRIDE_URI` now overridable; forwards `LG_HOST`,
  `RR_TASKS`, `RR_BLAS_THREADS`, `LG_TORCH_THREADS`, `LG_WORKERS`, `LG_PORT_BASE`,
  `LG_PER_ENDPOINT_CONCURRENCY`, `BENCH_IDLE_S`; new `/warm` mount.

## 5. Runner — `run/matched8x4_ami.sh` (new)

- **Corpus:** AMI `ami_full` pin (sorted-ID committed order); first `N` (168;
  smoke used 8) measured, **last 2 as disjoint warm** (`TS3012c`, `TS3012d`),
  hardlinked into `~/bench_corpus_ami_m8/{measured,warm}` with the manifest and a
  `measured_order.txt` both drivers honor (no lexicographic slicing).
- **Preflight:** quiet box (0 containers, load logged) → hash-verify every
  measured file against the manifest before either arm.
- **Provenance:** git state, host/lscpu/compose version, image digests, LG
  `pip freeze` + python + ffmpeg version/hash, RR SDK version, RR Dockerfile sha,
  pin + order files, `run_config.txt`.
- **Samplers:** engine cgroup at 15 s with 7 columns (`ts, cpu_usage_usec,
  memory.current, pids, anon, memory.peak, file cache`); per-PID RSS of all
  processes every 60 s (`rss_by_pid.log`); **driver container sampled
  separately** (`driver_cgroup.csv`).
- **Census watcher:** answers the driver's snapshot requests with the full
  process table (minus the snapshot shell tree) and, when asked, each process's
  six thread vars from `/proc/<pid>/environ`.
- `ARM_ORDER` (`rr lg` | `lg rr`) and `REP` label for alternating repetitions;
  20 s settle between arms; one `FINAL STATUS` line, any component nonzero → nonzero.

## 6. Report — `bench/report.py`

- Header prints `posture=`; new gates `task_census` (RR: 8/8 task processes,
  8 project ids, tokens distinct, environ readback for 8) and `worker_census`
  (LG: 8 distinct pids with torch/interop/detect-concurrency readbacks) — hard,
  fail-closed. Default postures get no extra gate.
- Sampler loader reads **`engine_cgroup.csv` only** (the driver file beside it
  had been merged in, interleaving two cumulative counters → negative CPU).
- V4 adds `driver_cpu_s_in_window`, `driver_effective_cores` (never added to
  the arm) and `idle_burden_cores` (engine cores during the idle window).

## 7. Fixes found by the smoke iterations (all committed)

| symptom | cause | fix |
|---|---|---|
| runner idle 2 h, driver never started | `CW=$(census_watcher …)`: backgrounded loop kept the capture's stdout open | redirect the loop's output to `census/watcher.log` |
| driver would exit at import | `bench_video.py` guard rejected `ttl=0` as "≤ timeout" | 0 exempt (= no expiry); finite ttl still must exceed timeout |
| census saw only its own shell | `*python*` filter — engine binary is `./engine`, tasks are `node.py /tmp/task-*`; snapshot's `sh -c` text contained "python" | list all processes minus the snapshot shell tree; match the task signature; NUL-safe `xargs -0` |
| LG CPU negative | `driver_cgroup.csv` merged with `engine_cgroup.csv` | engine file only; driver CPU reported separately |
| LG warm-up ~20 min | 16 sequential warm requests | endpoints warmed in parallel (~3 min) |

## 8. Disclosed limits

- **How RocketRide serializes a model (verified in source; agrees with Ansh's
  WS-1 citation):** the detect *node* (`nodes/detect/IGlobal.py:81`
  `self.device_lock = make_device_lock()`, a real `threading.Lock` in local mode)
  wraps every call — `IInstance.py`: `with self.IGlobal.device_lock:
  detector.detect(image)`. One task process = one detector = one lock, so
  inference is one-at-a-time per task. (`make_device_lock` has no callers under
  the engine's `ai/` package — it is used by the `nodes/` package, which is why
  an `ai/`-only search missed it.) Independently, a task is a single asyncio
  event loop, and the `threads=` argument becomes `asyncio.Semaphore(threadCount)`
  in `modules/data/data_conn.py` — an in-flight *items* cap, not parallel
  workers. Either way: parallelism inside a task comes only from torch's
  intra-op threads within a forward pass; that is the per-task ceiling (flat
  across threads=8…64) and why more tasks scale. Whisper (audio lane, not in
  this pipe) additionally holds a per-model lock. The LangGraph matched cell's
  per-worker predict lock is therefore an exact match on inference
  serialization; RocketRide additionally serializes the rest of the item
  pipeline on its loop, so LangGraph matched is the slightly more permissive.
- RocketRide torch **inter-op** threads inside engine task processes cannot be
  set or read (no env var; no code runs inside the task). Intra-op follows
  `OMP_NUM_THREADS=4` (verified via environ). Practically moot for RF-DETR's
  eager forward pass; LangGraph's 4/1 is fully read back.
- Realized work parity: on AMI frames are exact (smoke: 8/8 identical); on the
  films corpus the arms differ ~1.5% at the frame-decoder/serialization level
  (not the splitter) — reported as "equal within 1.5%", never "identical".
- 8×4 is the architecture-matched cell; it is not Ansh's 16×2 (29.4 cores).
  Pre-declared follow-up if RR utilization lands well under ~80%.
- Batch vs per-request semantics: throughput/CPU/cost are comparable under
  saturation; per-video latency and TTFR are not compared across arms.

## 9. Smoke result (N=8, 6.4 h, one video per process — null test, not saturation)

| | RR `rr_matched_8x4` | LG `lg_matched_8x4_c32` |
|---|---|---|
| verified processes | 8 tasks (pids 672…4231), environ 4×6 | 8 workers, torch 4/1, ckpt `d8f70210e425` |
| throughput | 160.1× | 185.4× |
| effective cores (windowed) | 25.1 (78.5%) | 23.9 (74.6%) |
| CPU per footage-min | 9.42 s | 7.73 s |
| peak memory | 23.9 GB (~2 GB per task) | 12.5 GB |
| idle burden | **5.4 cores** (8 idle tasks) | ~0 |
| parity | frame_parity 8/8 exact, workload_ratio 1.021, all gates PASS | |

RocketRide moves from ~6 cores (default) to 25 cores (matched) on the same corpus.

## 10. Commands

```bash
# smoke (done):      N=8   bash run/matched8x4_ami.sh
# full rep 1:              bash run/matched8x4_ami.sh            # N=168, ARM_ORDER="rr lg"
# reps 2-3 (alternate):    REP=2 ARM_ORDER="lg rr" bash run/matched8x4_ami.sh
#                          REP=3 ARM_ORDER="rr lg" bash run/matched8x4_ami.sh
```
