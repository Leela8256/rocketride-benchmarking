# RUN_MATCHED6x5_WALKTHROUGH.md — how the 6×5 pair run works, phase by phase

The run in flight (launched 2026-08-26, commit `6803a36`, log `~/logs/m6x5.log` on the
box, results `results/matched6x5-ami-rep1-<stamp>/`, mirrored to
`s3://rocketride-benchmark-data/leela/videobench/matched6x5-ami-rep1-<stamp>/`).

```bash
ARM_ORDER="rr lgip" RR_TASKS=6 RR_BLAS_THREADS=5 RR_THREADS_PER_CONN=4 RR_INFLIGHT_PER_TASK=4 \
LG_TORCH_THREADS=5 LG_WORKERS=1 LG_PORT_BASE=8200 LG_PER_ENDPOINT_CONCURRENCY=24 LG_EXPECT_DETECT_CONC=6 \
bash run/matched8x4_ami.sh
```

## 0. The question this run answers

Same corpus, same math budget (6 concurrent inferences × 5 torch threads = 30), same
in-flight width (24 videos), same per-model rule (one inference at a time). The only
thing left different is **process topology**: RocketRide reaches six concurrent
inferences with **six task processes and six model copies**; LangGraph reaches it with
**one ordinary uvicorn process and one shared model copy**.

| | RocketRide `rr_matched_6x5` | LangGraph `lg_inproc_6x5_c24` |
|---|---|---|
| container | `videobench-rocketride-m8`, 58 GB, all 32 cores | `videobench-langgraph-ip`, 58 GB, all 32 cores |
| processes / model copies | 6 tasks / 6 × (RF-DETR + MiniLM) | 1 uvicorn / 1 × each, shared |
| concurrent inferences | 6 (one per task; detect-node device lock) | 6 (inference semaphore around `predict`) |
| torch per inference | 5 intra-op (six BLAS/OMP vars = 5) | 5 intra-op / 1 inter-op |
| in-flight width | `use(threads=4)` × 6 connections = 24, engine-enforced; plus a client semaphore of 4 per task | client c24 to one endpoint |
| ingestion | native `send_files` per video, shard = index % 6 | native HTTP POST, whole backlog offered |
| evidence grade | SIZING (single rep, unpinned) | SIZING |

## 1. Launch mechanics

`box.sh launch` runs the runner under `nohup` on the box via SSM; the runner writes
`~/logs/m6x5.log`. A one-core **keepalive** busy loop starts with the runner (the box's
idle-watchdog stops the machine during low-CPU phases like hashing) and is **SIGSTOPped
for the entire arms phase** — it never taxes a measured span — then resumed for the
final sync.

## 2. Phase 1 — quiet-box preflight

Refuses to start if any container is running; logs the 1-minute load average.

## 3. Phase 2 — corpus split (no bytes copied)

From the committed AMI pin (`corpus/sets/ami_full.txt`, sorted-ID order): the **first
168** meetings are hardlinked into `~/bench_corpus_ami_m8/measured/` with a
`measured_order.txt` both drivers honor; the **last 2** (TS3012c, TS3012d) into
`~/bench_corpus_ami_m8/warm/` as disjoint warm fixtures. The corpus manifest (shas,
probed durations) is copied beside both.

## 4. Phase 3 — hash verification

Every measured file is SHA-256'd (8 parallel) and compared with the manifest's sha and
byte count. Any miss aborts before either arm. Written to `preflight_hashes.json`.

## 5. Phase 4 — build + provenance

Images are (re)built from cache; `provenance/` captures git state, host/lscpu/compose
version, image digests, LangGraph `pip freeze` + Python + ffmpeg version/hash, RocketRide
SDK version, the RocketRide Dockerfile sha, the pin and order files, and `run_config.txt`
with every knob above. A background loop syncs the results directory to S3 every 60 s.

## 6. Phase 5a — RocketRide arm (`run_rr`)

1. `docker compose up -d rocketride-matched` — engine container with the six BLAS/OMP
   vars = **5** (parameterized by `RR_BLAS_THREADS`), 58 GB limit. Wait for healthy.
2. Samplers start: engine cgroup every 15 s (`cpu.stat usage_usec`, `memory.current`,
   `pids`, anon, `memory.peak`, file cache), per-PID RSS every 60 s, and the **driver
   container sampled separately** the moment it appears. The **census watcher** starts:
   it answers the driver's snapshot requests with the engine container's full process
   table (minus the snapshot shell) and, on request, each process's six thread vars from
   `/proc/<pid>/environ`.
3. The driver (`bench/bench_video_matched.py`) runs in the client container:
   - writes 6 pipe copies with `project_id = videobench-matched6x5-00 … 05`;
   - opens **6 client connections**; on each, `use(filepath=<copy>, ttl=0, threads=4)`
     — no `use_existing`; `threads=4` is the per-connection admission cap;
   - after **each** `use()`, requests a census snapshot: exactly **one** new
     `engine … node.py /tmp/task-*.json` process must appear, and its environ must carry
     all six vars at 5 — otherwise all tokens are terminated and the run aborts before
     warm-up. Pids, cmdlines, project ids and redacted token digests go to
     `task_census.json`;
   - **warm-up**: both fixtures to every task concurrently; each must return both docs;
   - **idle window**: 30 s of nothing, with epoch markers, so the report can quote the
     CPU six idle tasks burn;
   - **sharded blast**: `shard = index % 6` (28 videos per task, committed order, no
     longest-first); at a barrier, six coroutines start; each keeps **4 videos in flight**
     on its task via a semaphore (`RR_INFLIGHT_PER_TASK=4`) using native `send_files`
     per video — 24 box-wide, everything else waits at the client;
   - `measurement_start_epoch_ns` is stamped immediately before the barrier releases,
     `measurement_end_epoch_ns` after the last shard's last result;
   - per-doc records: manifest index, token index, project id, task pid, shard-local
     index, timings, frame/detection/chunk counts, sha; explicit engine errors take
     precedence over "no documents";
   - `shot_meta` carries the posture, census, warm records, idle window, markers,
     `threads_arg`, admission width, in-flight policy, and provenance; tokens are
     terminated at the end.
4. Engine logs are saved; the container is stopped and removed (its scratch with it).

## 7. Phase 5b — LangGraph in-process arm (`run_lgip`)

1. `docker compose up -d langgraph-inproc` — the **ordinary single-uvicorn server**
   (image CMD unchanged: `uvicorn service:app --port 8200`) with three knobs turned by
   env: six vars = 5, `LG_TORCH_THREADS=5` / `LG_TORCH_INTEROP=1` (applied in the app's
   startup **before** the models load), `LG_DETECT_CONCURRENCY_PER_PROCESS=6` (a
   semaphore around the `predict` call only — extraction, chunking and embedding still
   overlap). One compiled graph, one RF-DETR, one MiniLM. Wait for `/health/ready`
   (models loaded + one synthetic frame through the whole graph).
2. Samplers start (same three as the RocketRide arm).
3. The driver (`bench/lg_driver_matched.py` with `LG_WORKERS=1`, port 8200):
   - **readback gate** via `/meta`: exactly one pid, `torch.get_num_threads() == 5`,
     `get_num_interop_threads() == 1`, six env vars = 5, graph compiled, both models
     loaded, checkpoint hash present, `detect_concurrency_per_process == 6` — any miss
     aborts;
   - **warm-up**: both fixtures to the process (the shared model is what needs warming;
     the six semaphore slots are the same model);
   - **idle window**: 30 s with markers;
   - **c24**: an explicit 24-thread executor; every request created, then released at a
     barrier; at most 24 requests open at once against the single endpoint, of which at
     most 6 are inside `predict` and the rest are decoding frames to disk, waiting for a
     slot, chunking or embedding; the serving pid is recorded per video;
   - markers stamped at barrier release and last completion; `shot_meta` records
     configured/observed concurrency, worker readbacks, warm records, idle window.
4. Service logs saved; container stopped and removed.

## 8. Phase 6 — report, sync, exit

`bench/report.py --arms results/…/rr results/…/lg`:
- per arm: census (all docs ok), structure (384-d finite normalized), frame_law,
  self_duplication, corpus_pin (fail-closed sha check), **task_census** (RR: 6/6) /
  **worker_census** (LG: 1 pid, readbacks), metric_coverage;
- CPU from the engine cgroup **windowed by the driver markers** (warm-up, boot and idle
  window excluded; ±15 s interpolation); driver CPU and idle-burden cores reported
  separately in V4;
- V1 throughput (frames/s, × realtime on probed footage), V2 latency kept per-arm
  (batch completion vs HTTP latency are never compared), V3 CPU per frame/detection/
  chunk/footage-minute + effective cores, V4 memory/idle burden/stage split, V5 cost;
- cross-arm: input identity, frame parity, detection ratio, chunk ratio, tight chunk
  parity, workload ratio; determinism NOT_RUN (single rep) → `evidence_grade: SIZING`.

Then the final S3 sync and one line: `FINAL STATUS matched6x5-ami-rep1-<stamp>:
rc_rr=… rc_lg=… rc_report=… rc_sync=…` — any nonzero component makes the runner exit
nonzero. The keepalive is resumed for the sync and released at exit; the box's idle
watchdog stops the machine afterwards.

## 9. Watching it

- log: `bash local/box.sh run 'tail -40 ~/logs/m6x5.log'`
- RocketRide progress: `results/…/rr/progress.jsonl` (one line per completed video with
  its token index); LangGraph: `results/…/lg/progress.jsonl`
- live cores: last rows of `results/…/rr|lg/engine_cgroup.csv`
- expected: RocketRide arm ~35–40 min, LangGraph arm ~40–45 min, ~1.5 h in all

## 10. How to read the result

Throughput, effective cores, CPU per frame and cost are directly comparable between the
two columns; per-video latency is not. Because budget, concurrency and width are all
matched, a difference is attributable to process topology — six copies vs one — and its
consequences (memory, per-task idle burden, inter-process overhead) rather than to how
much machine each side was allowed.
