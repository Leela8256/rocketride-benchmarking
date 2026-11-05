# aws_video: the video benchmark (RocketRide vs LangGraph)

Self-contained: both arms, the pipeline contracts, the corpus tooling, the
drivers, the gates and metrics, and every runner. Laptop-side box control
is `../local/box.sh`; box access is described in `../docs/AWS_BOX_ACCESS.md`.

## 1. What is measured

One pipeline, executed by RocketRide from `pipe/benchmark_video_detect.pipe`
and mirrored stage for stage by the LangGraph service in `arms/langgraph/`:

| stage | RocketRide | LangGraph | config |
|---|---|---|---|
| ingest | `webhook`, `video` lane | FastAPI `POST /process` (multipart) | file upload |
| frames | `frame_grabber`, interval profile | `frames` (`ffmpeg -vf fps=1/15`, lossless PNG) | one frame every **15 s** (about 121 frames per 30-min meeting) |
| detect | `detect`, profile `rfdetr` | `detect` (`rfdetr.RFDETRBase`) | threshold **0.3**; one JSON array per frame: `[{"label","score","box":{x1,y1,x2,y2},"centroid":{x,y}}, ...]` |
| chunk | `preprocessor_langchain` | `chunk` | RecursiveCharacterTextSplitter, **4000 chars, 0 overlap**, over newline-joined frame lines |
| embed | `embedding_transformer`, `miniLM` | `embed` | `multi-qa-MiniLM-L6-cos-v1`, 384-d, L2-normalised |
| respond | `response_documents` | response JSON | chunks + vectors |

`pipe/benchmark_video.pipe` is the dual-lane variant that adds
`audio_transcribe` (faster-whisper base). It carries a known determinism
caveat (Whisper flips about one segment per 20 minutes across reps) and is
not the benchmark contract. The one-video smoke uses it to exercise the
audio path.

The detector is the cost centre on both arms (about 90 % of the time under
concurrency). Per-frame CPU is the same within 10 % between arms; the
throughput gap between postures is how many inferences run at once.

## 2. Folder map

```
aws_video/
  docker-compose.yml           both arms + the client; default posture services plus
                               rocketride-matched, langgraph-matched, langgraph-workers8, langgraph-inproc
  docker-compose.official.yml  override: run the official 3.5.0.584 engine image instead of 3.3.1
  pipe/                        benchmark_video_detect.pipe (THE contract), benchmark_video.pipe (dual-lane)
  arms/rocketride/             engine 3.3.1 image, sha-pinned + boot fix + duplication correction
  arms/langgraph/              service.py graph.py serve8.py workload/ Dockerfile requirements.txt smoke_lg.py
  bench/
    bench_video.py             RocketRide driver, default posture (blast / seq / c<N>)
    lg_driver.py               LangGraph driver, default posture (seq / c<N>); SAME record schema
    bench_video_matched.py     RocketRide driver, matched posture (K tasks, sharded blast, task census)
    lg_driver_matched.py       LangGraph driver, matched posture (K endpoints, readback gate, barrier)
    lg_driver_1port.py         LangGraph driver, one-port multi-worker cell
    capture_one.py             full-output capture for verification passes
    report.py                  gates first, numbers second, fail-closed
    metrics/v0_gates.py        V0 gates; metrics/v_metrics.py V1 to V5
    flow_selftime.py           exclusive self time from the engine's flow traces
  corpus/
    fetch_ami.sh               AMI mirror -> mux -> run-ready .avi (+ manifest, sha256)
    sets/ami_full.txt          all 170 usable AMI meetings, sorted-ID committed order
    sets/ami_full_durations.json, sets/ami30h.txt
    sets/archive_films_*.txt   the Internet Archive film corpus pins (10, 50, 100, 500)
  run/                         on-box entrypoints, each self-documenting (see section 5)
  smoke/                       the client image (rocketride SDK + requests) and the one-video smoke driver
  findings/rocketride_cpu_utilization.md   the ~6-core-per-task finding, in plain language
  DATA_FLOW_PLAN.md            how data moves: mirror -> S3 -> RAM -> engine -> records -> S3
  METRICS.md, METRICS_EXPLAINED.md         the V0 to V5 suite
  MATCHED_POSTURE.md           the matched posture, config by config
  SETUP_AND_RUN.md             the 2026-08-21 operating picture (default postures)
  RUN_MATCHED6x5_WALKTHROUGH.md            a matched pair run, phase by phase
  RESULTS*.md, ARCHIVE_FILMS*.md, LONG_VIDEO_SOURCES.md   results and corpus notes
  results/, runs/              run output, git-ignored; S3 is the record
```

## 3. The one rule

**Only the framework may vary between arms.** Same corpus, same order, same
envelope, same reps, same mode, same deadline, same client. One arm at a
time, one engine per arm. Provenance or it did not happen.

## 4. Corpora

| set | contents | where | how it got there |
|---|---|---|---|
| `ami_full` | 170 muxed AMI meetings, about 24 GB, 99.5 h; manifest with per-file sha256, audio duration and ffprobe video duration | `s3://rocketride-benchmark-data/leela/corpus/ami_full/` | `run/stage_corpus.sh` (mirror -> mux -> probe -> S3, resumable, about 2 h); **already done, reuse it** |
| `ami30h`, `ami30test` | 62 and 30 meeting subsets | same bucket, `corpus/ami30h/`, `corpus/ami30test/` | earlier staging |
| `archive_films_v2` | 500 public-domain feature films, 262 GiB, 677 h, sealed manifest | `corpus/archive_films_v2/` | `run/stage_films_jit.py` (see `ARCHIVE_FILMS_S3_AND_RUNBOOK.md`) |

A benchmark document is one `.avi` with sound: the AMI mirror ships the
camera file without audio, so staging stream-copies `Closeup1.avi` and
`Mix-Headset.wav` into one file (bitexact flags, metadata stripped).
`run/native170.sh` pulls the corpus from S3 to `~/bench_corpus_ami_full` on
first use (about 3 minutes at 158 MB/s); `run/matched8x4_ami.sh` does not,
and exits with `FATAL: no corpus manifest` if that directory is missing, so
run `native170.sh` once or pull it by hand:
`aws s3 cp --recursive s3://rocketride-benchmark-data/leela/corpus/ami_full/ ~/bench_corpus_ami_full/`.
Both runners verify every measured file's sha256 against the manifest before
either arm runs. Stage from the mirror only if the S3 copy is gone.

## 5. Run it

On the box, from a clone of this repository, in `aws_video/`. Every runner
is launched detached from the laptop, on one line:

```bash
bash local/box.sh launch <name> 'cd ~/rocketride-benchmarking/aws_video && <ENV> bash run/<runner>.sh'
bash local/box.sh tail <name>
```

### 5.1 First run: a small native run of both arms (about 25 minutes)

```bash
RUN_NAME=smoke-n4-$(date -u +%Y%m%dT%H%M%SZ) N=4 WARM=2 LG_MODE=c4 bash run/native170.sh
# expected: FINAL line rr=0 lg=0 report=0; determinism SKIP (single rep, evidence_grade SIZING), every other gate PASS
```

Pulls `ami_full` from S3 if missing, builds `rocketride`, `langgraph` and
`smoke`, runs the RocketRide arm (blast) then the LangGraph arm (c4) on the
first 4 meetings with 2 warm-ups, samples the engine cgroup every 15 s,
writes `report.txt` with cross-arm gates, syncs to S3 every 60 s. The first
RocketRide `use()` on a fresh model-cache volume installs the video nodes'
dependencies and downloads weights (minutes); that is warm-up.

### 5.2 One-video pipe smoke (dual-lane, audio included)

```bash
bash run/smoke_run.sh            # fetches ES2002a from the AMI mirror, 2 reps through benchmark_video.pipe
```

Turns the response into explicit verdicts: lane routing, audio demux and
transcription, frame lane, structure, content determinism, order
determinism. Rep 1 on a cold engine is init plus work and is never a number.

### 5.3 Default posture, full corpus (about 3.5 h)

```bash
bash run/native170.sh                             # N=168 WARM=2, RR blast, LG c170, both arms
LG_MODE=c32 bash run/native170.sh                 # Run C itself used c32 for the LangGraph arm
NATIVE_ARMS=rr bash run/native170.sh              # one arm only
RR_LENSORT_PATCH=1 NATIVE_ARMS=rr bash run/native170.sh   # engine A/B
```

Knobs: `N` (168), `WARM` (2, a count), `RR_MODE` (blast), `LG_MODE` (c170),
`NATIVE_ARMS` ("rr lg"), `CORPUS_DIR`, `RUN_NAME`, `BENCH_TIMEOUT_S` (21600),
`RR_THREADS` (recorded; no effect on a single task's utilisation).

### 5.4 Matched posture (architecture-matched cells; about 1.5 h for a pair)

```bash
bash run/matched8x4_ami.sh                                    # RR 8 tasks x BLAS 4, then LG 8 procs x torch 4, c32
ARM_ORDER=rr bash run/matched8x4_ami.sh                       # RocketRide cell only
ARM_ORDER=lg1 PAIR_RR=results/<rr run>/rr bash run/matched8x4_ami.sh   # LG one-port cell, paired with an earlier RR cell
ARM_ORDER="rr lgip" RR_TASKS=6 RR_BLAS_THREADS=5 RR_THREADS_PER_CONN=4 RR_INFLIGHT_PER_TASK=4 LG_TORCH_THREADS=5 LG_WORKERS=1 LG_PORT_BASE=8200 LG_PER_ENDPOINT_CONCURRENCY=24 LG_EXPECT_DETECT_CONC=6 bash run/matched8x4_ami.sh   # the 6x5 pair: 6 engine tasks vs 1 in-process uvicorn
```

| variable | default | meaning |
|---|---|---|
| `N` | 168 | measured meetings, first N of `corpus/sets/ami_full.txt`; the last 2 are the disjoint warm fixtures |
| `REP` | 1 | label only; alternate `ARM_ORDER` between reps |
| `ARM_ORDER` | `rr lg` | any of `rr`, `lg` (8-port matched), `lg1` (one-port 8 workers), `lgip` (in-process) |
| `RR_TASKS` | 8 | engine tasks: pipe copies with distinct `project_id`, one `use()` each on its own connection, no `use_existing`, `ttl=0` |
| `RR_BLAS_THREADS` | 4 | the six BLAS/OMP variables on the engine container; verified in each task's environ by the census |
| `RR_THREADS_PER_CONN` | unset | `use(threads=N)` per connection (engine admission cap, 1 to 64); admission width = tasks x threads |
| `RR_INFLIGHT_PER_TASK` | 4 | videos in flight per task, client-bounded; 0 = one native batch per shard |
| `LG_WORKERS`, `LG_PORT_BASE` | 8, 8201 | uvicorn processes and first port |
| `LG_TORCH_THREADS` | 4 | torch intra-op per process (inter-op 1) |
| `LG_PER_ENDPOINT_CONCURRENCY` | 4 | active requests per endpoint; client concurrency = workers x this |
| `LG_EXPECT_DETECT_CONC` | 1 | predicts allowed per process; the readback gate checks it |
| `PAIR_RR`, `PAIR_LG` | unset | an earlier arm directory to pair with, for a cross-arm report |
| `BENCH_TIMEOUT_S` | 86400 | one deadline |

What the matched runner does, in order: quiet-box preflight (refuses if
any container is running); hardlinks the measured and warm sets from
`~/bench_corpus_ami_full`; sha256-verifies every measured file; builds only
the arms it will run; captures provenance (git state, host, image digests,
`pip freeze`, ffmpeg hash, SDK version, Dockerfile hash, pins, run config);
starts a 60 s S3 sync; runs the arms with a cgroup sampler (15 s), a
per-PID RSS sampler, the driver container sampled separately, and (RR) a
census watcher that answers the driver's process-table requests; stops and
removes each container; writes the paired or single-arm report; final sync;
one `FINAL STATUS` line. The RocketRide driver aborts before warm-up if the
census does not show exactly one new task process per `use()` with the six
variables set. Full detail: `MATCHED_POSTURE.md`, `RUN_MATCHED6x5_WALKTHROUGH.md`.

### 5.5 Other runners

| runner | purpose |
|---|---|
| `run/lg_config_sweep.sh` | LangGraph posture grid (processes x per-endpoint concurrency x torch threads) on a subset |
| `run/rr_threads_sweep.sh` | the `threads=` sweep on one task (result: flat) |
| `run/rr_best_configs.sh`, `run/lg_best_configs.sh`, `run/films_best_configs.sh` | the best cells of each arm |
| `run/nodeprofile_ami.sh` | per-node attribution: RR traced cell + control cell + LG per-node timings + per-video sequential cell |
| `run/films_v2.sh`, `run/films100_headtohead.sh`, `run/rr_films_postures.sh` | the film corpus runs (subsets 10, 50, 100, 500) |
| `run/run_waves.sh` | corpora larger than disk: S3 -> RAM -> engine -> S3, container removed per wave, resumable |
| `run/stage_corpus.sh`, `run/stage_films_jit.py`, `run/freeze_films.py` | corpus staging and sealing |
| `run/headtohead.sh`, `run/native60.sh`, `run/run_detect.sh`, `run/run30h.sh`, `run/run_s3test.sh`, `run/archive10.sh`, `run/capture_one.sh`, `run/smoke_lg_box.sh` | the earlier, smaller shapes; kept because results reference them |

### 5.6 Running on the official 3.5.0.584 engine

The video Dockerfile builds 3.3.1. To run the official build instead, make
sure the frozen image `bench-rocketride:official-87f530f` exists (build and
tag it from the PDF arm only if it is missing; the exact steps and the
limits of the override are in `../docs/DOCKER_AND_ENVELOPE.md` section 3),
then source the env file that selects the override compose file and run:

```bash
bash local/box.sh launch vid350 'cd ~/rocketride-benchmarking/aws_video && . run/official-engine-20261021.env && NATIVE_ARMS=rr bash run/native170.sh'
```

The override retargets the default `rocketride` service; verify the matched
service's image with `docker compose config --images` before using it with
`matched8x4_ami.sh`. `native170.sh` records no image id, so save
`docker image inspect bench-rocketride:official-87f530f` and the engine's
`/version` output next to the run.

## 6. Postures, named

| posture | arm | process topology | driver |
|---|---|---|---|
| `rr_default_1token` | RocketRide | 1 task (one `use()`, `use_existing=True`), `threads` unset, env untouched; about 6 of 32 cores | `bench_video.py` |
| `lg_native_1process` | LangGraph | 1 uvicorn, shared models, no inference lock, torch default | `lg_driver.py` |
| `rr_matched_8x4` | RocketRide | 8 tasks, BLAS 4, sharded blast | `bench_video_matched.py` |
| `rr_matched_6x4` (threads=32) | RocketRide | 6 tasks, `use(threads=32)` | same |
| `lg_matched_8x4_c32` | LangGraph | 8 uvicorn processes, one port each, torch 4/1, one predict at a time per worker, c32 | `lg_driver_matched.py` |
| `lg_workers8_1port_c32` | LangGraph | `uvicorn --workers 8` on one port | `lg_driver_1port.py` |
| `lg_inproc_6x5_c24` | LangGraph | 1 uvicorn, torch 5/1, inference semaphore 6 | `lg_driver_matched.py`, `LG_WORKERS=1` |

Why postures exist: a RocketRide task holds one detector behind a real lock
(`nodes/detect/IGlobal.py` `device_lock`, a `threading.Lock` in local mode),
so one task is about 6 cores no matter what `threads` says, and the engine
scales by tasks. The matched cells give both arms the same number of
concurrent inferences and the same math budget, so a remaining difference
is attributable to process topology.

## 7. What a run produces

```
results/<run>/
  rr/  per_doc.jsonl      one record per video (+ shot_meta line with mode, span, markers, provenance)
       progress.jsonl     one line per video as it finished
       engine_cgroup.csv  ts, cpu_usage_usec, memory.current, pids, anon (matched: + mem_peak, file cache)
       driver.log, service.log
       (matched) census/, task_census.json, rss_by_pid.log, driver_cgroup.csv
  lg/  the same files from the LangGraph driver, identical schema
  provenance/            (matched) git state, host, images, pip freeze, ffmpeg, SDK, pins, run_config.txt
  preflight_hashes.json  (matched) every measured file verified against the manifest
  report.txt             gates + V0 to V5; FINAL STATUS line in the launch log
```

Mirrored to `s3://rocketride-benchmark-data/leela/videobench/<run>/` every
60 s during the run. Re-derive anywhere:

```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/videobench/<run>/ ./run --profile <p>
python3 bench/report.py --arms ./run/rr ./run/lg
```

## 8. Gates and numbers

Per arm: `census`, `structure`, `frame_law`, `self_duplication`,
`corpus_pin`, `determinism` (single rep = NOT_RUN, reported as SKIP, and the
run is graded SIZING), `metric_coverage`, and in matched postures
`task_census` / `worker_census`. Across arms: `input_identity`,
`frame_parity` (PASS when identical, WARN within 10 %, FAIL beyond),
`detection_ratio` (warn outside 0.90 to 1.10), `chunk_ratio` (hard 0.4 to
2.5), `chunk_parity_tight` (warn), `workload_ratio`. Byte parity is
deliberately not required: the arms run different `rfdetr` package
generations and a handful of borderline detections flip.

Headline numbers: `x_realtime` (footage hours per wall hour),
`cpu_s_per_footage_min` (the fairest cross-arm number), `effective_cores`,
`usd_per_1k_footage_hours`. Latency is mode-labelled and never compared
across modes. `report.py` exits non-zero on any hard gate failure.

## 9. Reference documents

`RESULTS*.md`, `MATCHED_POSTURE.md`, `METRICS.md`, `METRICS_EXPLAINED.md`,
`SETUP_AND_RUN.md`, `DATA_FLOW_PLAN.md` and `LONG_VIDEO_SOURCES.md` were
written inside the original working repository. They cite `runs/` and
`results/` bundles that are not in this repository (use the S3 paths they
also give), and two of them mention a sibling document
(`VIDEO-METRICS-IMPLEMENTED.md`) and a script (`corpus/fetch_archive.sh`)
that were never part of this folder.

## 10. Traps specific to video

The engine keeps every uploaded video in container scratch until the
container is removed (teardown between arms is load-bearing). One wave must
fit in `/dev/shm` (about 30 GB) when using `run_waves.sh`. `WARM` is a count
for the runners and a bind mount for compose; the runners handle it, do not
export it yourself. Dense Idiap rooms exceed 4000 characters per frame line,
so chunks are text windows rather than valid JSON on both arms. The full
list is in `../docs/KNOWN_TRAPS.md`.
