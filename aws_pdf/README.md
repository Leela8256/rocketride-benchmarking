# aws_pdf: the PDF benchmark (RocketRide vs LangGraph)

Self-contained. Everything needed to build both arms, run a matched
benchmark on the AWS box, and derive every metric lives in this folder.
Laptop-side box control is `../local/box.sh`; box access is described in
`../docs/AWS_BOX_ACCESS.md`.

## 1. What is measured

One pipeline, executed by RocketRide from `pipe/benchmark_pdf.pipe` and
mirrored stage for stage by the LangGraph service in `arms/langgraph/`:

| stage | RocketRide | LangGraph |
|---|---|---|
| ingest | `webhook` | FastAPI `POST /v1/process/document-pdf-v1` (multipart) |
| parse | `parse` (engine-bundled Apache Tika 3.2.3) | `extract` via the Apache Tika 3.2.3 sidecar with RocketRide's `tika-config.xml` (`LG_EXTRACTOR=tika`, the default) or pinned pypdf 6.15.0 |
| chunk | `preprocessor_langchain`, RecursiveCharacterTextSplitter, library defaults | `chunk`: `RecursiveCharacterTextSplitter()` defaults, 4000 chars, 200 overlap |
| embed | `embedding_transformer`, profile `miniLM` | `embed`: `multi-qa-MiniLM-L6-cos-v1`, CPU, 384-d, normalised |
| respond | `response_documents` | response envelope with `X-Output-SHA256` and `Server-Timing` headers |

Corpus: GovDocs1 PDFs from Digital Corpora, selected deterministically
(first N by basename after an offset), fetched on the box, checksummed.

## 2. Folder map

```
aws_pdf/
  docker-compose.yml        both arms + tika + the bench client, ONE equal envelope
  arms/langgraph/           service/ pipelines/ workload/ Dockerfile Dockerfile.profile tika-config/
  arms/rocketride/          Dockerfile (pinned engine + documented patches), entrypoint, probe fixtures
  pipe/benchmark_pdf.pipe   THE shared contract; the client sends it with use()
  metrics/                  M0 to M7 and provenance: the only place a metric is computed
  bench/                    drivers (rr_driver.py, lg_driver.py), cgroup_sampler.py, report.py,
                            cross_mode_determinism.py, verify_output.py, merge_samplers.py,
                            profiler capture tools, bench.Dockerfile (the client image)
  corpus/fetch_govdocs.sh   corpus, downloaded on the box; make_faults.py for fault runs
  run/matched_run.sh        THE run: both arms, identical in every respect but the framework
  run/write_provenance.py   writes and checks provenance.json
  run/preflight.sh          read-only box check; run/install_awscli.sh
  run/official-engine-20261021.env   the pinned official 3.5.0.584 engine (default)
  run/batch10k_*.sh         the 10k batch campaigns (RR and LG) and their pairing/finalise tools
  run/nodeprofile.sh        per-node attribution with the engine's own flow traces (+ control cell)
  run/pdf1k_profile_campaign.py      the frozen-image profiler campaign (yappi), 2026-10-21
  run/parse_ab_aws.sh, parse_pathology.sh   parse-node A/B (PR #2746) and pathology attribution
  run/pr2197_pdf_ab.sh      embedding length-sort A/B (PR #2197)
  run/fault_run.sh          fault injection for M4 / M5
  findings/rr_linux_boot.md why 3.3.x needs the onnxruntime pin fix to boot on Linux
  METRICS_SUITE.md          the metrics contract; metrics/README.md has the per-module logic
  RESULTS_PDF.md            the PR #2197 A/B write-up (an example of a complete results doc)
  results/                  run output, git-ignored; S3 is the record
```

## 3. The one rule

**Only the framework may vary between arms.** Same corpus, same N, same
document order, same envelope, same rep count, same mode, same warm-up,
same deadline, the same client driving both. `run/matched_run.sh` enforces
every one of these and records them in `provenance.json`. A run whose
provenance is incomplete is not publishable.

## 4. Run it

On the box, from a clone of this repository:

```bash
cd ~/rocketride-benchmarking/aws_pdf
bash run/preflight.sh                      # x86_64, 32 cores, docker, disk, network (read-only)
bash run/install_awscli.sh                 # once; then symlink aws into /usr/local/bin (docs/AWS_BOX_ACCESS.md)
```

First run, a smoke of both arms (about 20 minutes, most of it image builds):

```bash
N=10 REPS=1 WARM=2 MODE=c4 bash run/matched_run.sh
```

The real thing, launched detached from the laptop:

```bash
bash local/box.sh launch pdf200 'cd ~/rocketride-benchmarking/aws_pdf && N=200 REPS=3 MODE=c8 bash run/matched_run.sh'
bash local/box.sh tail pdf200
```

`matched_run.sh` fetches or verifies the corpus for `N`, builds the images,
runs the LangGraph arm (with Tika) for `REPS` repetitions, stops it, runs the
RocketRide arm for `REPS` repetitions, writes provenance, derives the report,
and uploads the run directory to S3. Its exit status is the gate verdict.

### Knobs (all recorded in provenance)

| variable | default | meaning |
|---|---|---|
| `N` | 200 | measured documents |
| `REPS` | 3 | repetitions per arm; 3 is the minimum for a coefficient of variation |
| `WARM` | 25 | disjoint warm-up documents per rep, timed separately, excluded |
| `MODE` | `blast` | `seq` (one at a time), `c<N>` (closed loop, N in flight), `blast` (whole corpus at once), `b<B>` (consecutive batches of B), `native_saturation` (LG `c<LG_CLIENT_WINDOW>`, RR `blast`) |
| `LG_CLIENT_WINDOW` | 128 | the LangGraph window in `native_saturation` |
| `CORPUS_OFFSET` | 0 | skip the first OFFSET documents, for a disjoint set (the 10k campaign used 200) |
| `CORPUS` | `~/bench_corpus_n<N>_off<OFFSET>` | derived from N and OFFSET so the two cannot drift |
| `MAX_ARCHIVES` | 10 | govdocs zip archives to consume; the 10k corpus needs 50 |
| `ARM_CPUS` | 3/4 of host (24) | cores for the arm; the client gets the rest on its own cores |
| `BENCH_CPUSET`, `CLIENT_CPUSET` | derived | override for the SMT-isolated posture `0-11,16-27` / `12-15,28-31` |
| `BENCH_TIMEOUT_S` | 3600 | one outer deadline for both arms |
| `SAMPLE_MAX_S` | 86400 | sampler lifetime; the report discloses coverage |
| `LG_EXTRACTOR` | `tika` | `tika` matches RocketRide's parser; `pypdf` is the offline-reference path |
| `RR_THREADS` | unset | `use(threads=N)`; unset = engine default; recorded as requested, never as activated |
| `ENGINE_VERSION`, `ENGINE_URL`, `ENGINE_SHA256`, `ENGINE_ARCHIVE_SHA256` | official 3.5.0.584 (sourced from `run/official-engine-20261021.env` when both URL and VERSION are unset) | which engine tarball to build |
| `RR_DUP_PATCH` | 1 (Dockerfile); 0 in the official env | the 3.3.1 duplication correction |
| `RR_LENSORT_PATCH` | 0 | PR #2197 A/B |
| `PARSE_STOCK_URL` | empty | parse-node overlay for the PR #2746 A/B |
| `SKIP_LG`, `SKIP_RR` | 0 | run one arm only (a probe, not a comparison; the report says so) |
| `CENSUS_EMPTY_POLICY` | `fail` | `report` names text-free documents without failing |
| `RR_TRACE` | empty | `metadata`, `summary` or `full`: per-node timing from the engine's flow traces (needs a control cell) |
| `RR_PROFILE_SECONDS`, `LG_PROFILE_SECONDS` | 0 | profiler capture windows; nonzero makes the run diagnostic |
| `RUN_TAG` | stamp | names the results directory and the S3 prefix |
| `BENCH_S3` | `s3://rocketride-benchmark-data/leela/bench` | upload root |

Long runs must never sit in an SSM session; use `box.sh launch`.

### Modes, precisely

| mode | LangGraph | RocketRide |
|---|---|---|
| `seq` | 1 request at a time | 1 document per SDK call |
| `c<N>` | N-wide HTTP window | N in flight, one SDK call per document |
| `b<B>` | waves of B concurrent requests joined at a barrier | consecutive `send_files` batches of B on one connection |
| `blast` | window = corpus size | ONE whole-corpus `send_files` batch |
| `native_saturation` | `c<LG_CLIENT_WINDOW>` | `blast` |

`native_saturation` runs each arm's own ingestion path because they are not
the same interface. Fairness lives in the equal cpuset, not the interface;
a run in this mode must never be described as equal-submission or
equal-concurrency. `b<B>` shares the shape of the offered load, not the wire
protocol.

## 5. What a run produces

```
results/<RUN_TAG or STAMP_MODE>/
  environment.txt           host, kernel, docker, git, every knob
  image_ids.txt             image id per arm
  meta_lg.json              the LangGraph /meta (workload versions, pins)
  pip_freeze_lg.txt         the LangGraph arm's resolved packages
  engine_sha.txt            sha256 of the engine binary that ran
  engine_boot.log, engine_run.log
  corpus.sha256, corpus_manifest.json
  lg/rep<r>/per_doc.jsonl   one record per document (+ shot_meta line)
  lg/rep<r>/sampler.jsonl   0.5 s cgroup samples of the langgraph container
  lg/rep<r>/sampler_tika.jsonl   same for the Tika sidecar (charged to LangGraph)
  rr/rep<r>/per_doc.jsonl, sampler.jsonl, pipeline.pipe
  provenance.json           written and checked by run/write_provenance.py
  report.txt                gates, per-rep metrics, stability, cross-arm
```

The whole directory is copied to `BENCH_S3/<RUN_TAG or STAMP>/`. From any
machine:

```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/bench/<run>/ ./run --profile <p>
python3 bench/report.py ./run
```

## 6. The correctness gate

Per arm, fail-closed (a missing field or unproven state is a violation):

| check | catches |
|---|---|
| `census` | loss: offered == records, no duplicates, no unexpected failures, missing documents named against the manifest |
| `structure` | corruption: identity proven (RR filepath echo; LG request id + `X-Output-SHA256`), at least one chunk, 384 dims, finite, L2 norm within 1e-3 of 1, hash count == chunk count |
| `determinism` | instability: ordered chunk hashes identical across reps; an outcome that flips also fails. Single-rep runs need `bench/cross_mode_determinism.py` evidence (two modes or two cells on the same documents) |
| `self_duplication` | the 3.3.1 double-emit defect class; needs no other arm |

Across arms: chunk ratio per document (hard 0.4 to 2.5, warn 0.8 to 1.25)
and byte parity of ordered chunk hashes (reported; required for
`comparison_eligible`). `RUN PASS` covers execution, per-arm M0 and
provenance; `comparison_eligible` additionally requires both arms, cross-arm
parity and an unprofiled run.

## 7. What to look at, and what not to quote

- `cpu_s_per_chunk` is the soundest single number: work-normalised,
  tail-robust.
- `chunks_per_s` beside `docs_per_s`, never `docs_per_s` alone.
- Latency labels are not interchangeable: closed loop gives service
  latency; blast and batches give batch-position latency.
- `INSUFFICIENT_REPS` or `UNSTABLE` means do not quote a point value.
- `cpu_utilization` is against the arm's allocation; `cpu_utilization_host`
  is beside it for capacity questions. Both are span-averaged; read the
  distribution.
- `RR_THREADS` requested is not threads activated and is not effective cores.

## 8. The envelope, in short

Every container in an arm shares one `BENCH_CPUSET` (LangGraph's arm is
`langgraph` plus `tika` together). Memory is measured, not capped. One
outer deadline for both arms. Tika is stopped before RocketRide starts. The
client runs on `CLIENT_CPUSET`. Intra-op threads are pinned to 1 on both
arms. The full reasoning is in `../docs/DOCKER_AND_ENVELOPE.md`.

## 9. Campaigns that exist (each runner is self-documenting)

| runner | what it does |
|---|---|
| `run/batch10k_campaign.sh` | RocketRide only, 10k documents, `b16`, `b32`, `b64`, cross-cell determinism, regenerated reports, S3 |
| `run/batch10k_lg_campaign.sh` | the LangGraph twin |
| `run/batch10k_pair_reports.sh`, `run/batch10k_finalize.sh` | pair RR and LG cells into cross-arm reports; merge supplementary samplers |
| `run/nodeprofile.sh` | traced cell (`RR_TRACE=summary`) plus untraced control; per-node attribution |
| `run/parse_pathology.sh`, `run/parse_ab_aws.sh` | which node the catastrophic documents belong to; stock vs patched parse node |
| `run/pdf1k_profile_campaign.py` | one profiled run per arm on frozen images, SMT-isolated cpusets, cgroup sampling from the host |
| `run/pr2197_pdf_ab.sh` | stock vs length-sorted embedding batching |
| `run/fault_run.sh` | corrupt, encrypted and zero-byte documents at known positions for M4 and M5 |

## 10. Profiler diagnostics (2026-10-21 campaign)

`run/pdf1k_profile_campaign.py --reps 0` runs one profiled run per
framework, LangGraph then RocketRide, on the 1,000-document manifest at
offset 200 with 24 disjoint warm-up documents and 32-document waves. Each
cell starts a fresh service; only one arm runs at a time; image IDs, the
pipe and the Tika configuration are frozen before launch; the host samples
cgroup counters outside the measured services; both arms mount the same
read-only offline MiniLM cache.

For LangGraph, `arms/langgraph/Dockerfile.profile` derives from the audited
image and adds pinned yappi 1.7.6 plus an optional control entry point. With
`BENCH_PROFILE_CONTROL` disabled, yappi is not imported and the control
routes do not exist. For RocketRide, `RR_PROFILE_SECONDS=60` starts a
capture after warm-up through the SDK's `call()` method and exports it
before the task is terminated; `bench/profile_task.py --token TOKEN` can
attach to any running task from a client environment. Each profiled rep
writes `profiler/session.json`, `threads.json`, `report.txt`, `tree.json`
and per-thread JSON.

Rules: profiler percentages include waits and overlapping thread time, they
are not CPU shares; Python profiling does not resolve Java, Tika, native
engine loops, FFmpeg or GPU work; captures are diagnostic and their speed is
never quoted as unprofiled performance; a capture cut short by its safety
limit is reported as partial.

## 11. Known, and deliberate

- An arm is the service plus whatever it offloads to; Tika's CPU is charged
  to LangGraph as `m7_arm_total`, which feeds `cpu_s_per_chunk`.
- Tika time is in LangGraph's latency and throughput and always was: the
  extract node makes a blocking HTTP call inside the timed request.
- `/meta` reports `executor_workers: 4`, which is inert.
- The historical 3.3.1 image carries the duplication correction
  (`RR_DUP_PATCH`), guarded and labelled; describe it as "3.3.1 with the
  documented embedding-transformer duplication correction". The defect was
  fixed upstream from 3.4.0; the official 3.5.0.584 build runs unpatched and
  the `self_duplication` gate keeps watching for a regression.
- The RocketRide image patches a broken dependency pin so the engine boots
  on Linux at all (`findings/rr_linux_boot.md`).
- `000164.pdf` and `000357.pdf` return zero chunks on both arms; only the
  first is allowlisted, so use `CENSUS_EMPTY_POLICY=report` on govdocs runs.
- On this box CPUs 0-15 and 16-31 are SMT siblings; the historic `0-23` arm
  set shares eight physical cores with the client set. The SMT-isolated
  posture is a different hardware posture and needs fresh paired runs.
