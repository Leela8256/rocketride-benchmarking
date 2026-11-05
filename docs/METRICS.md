# Metrics

What we measure, exactly how each number is computed, and the rules that
decide whether a number may be quoted at all. Two suites, one discipline:
gates first, numbers second.

| suite | track | spec | code |
|---|---|---|---|
| M0 to M7 | PDF | `aws_pdf/METRICS_SUITE.md` (contract), `aws_pdf/metrics/README.md` (per-module logic) | `aws_pdf/metrics/*.py`, run by `aws_pdf/bench/report.py` |
| V0 to V5 | video | `aws_video/METRICS.md` (spec), `aws_video/METRICS_EXPLAINED.md` (every rule with measured values) | `aws_video/bench/metrics/v0_gates.py`, `v_metrics.py`, run by `aws_video/bench/report.py` |

If code and a spec document ever disagree, that is a bug in one of them. Fix
it; never reinterpret.

## 1. Where the raw data comes from

Every number originates in the **driver**, not in the framework. For each
document or video the driver records a monotonic timestamp when it submits,
another when the verified response is fully received, the outcome, the
ordered chunk hashes, the vector checks, and (video) frame and detection
counts. One JSON line per document: `per_doc.jsonl`. A final `shot_meta`
line carries the run's mode, span, warm-up stats, pipe hash and provenance
fields.

Alongside, a **sampler** records the arm's CPU and memory from the
container's own cgroup (`cpu.stat usage_usec`, `memory.current`,
`memory.stat anon`, `pids.current`): every 0.5 s inside the container for
PDF (`aws_pdf/bench/cgroup_sampler.py`), every 15 s from the host for video.
Never `/proc/stat`: it is not namespaced in Docker and counts idle time, which
made an earlier sampler report 17.8 cores for a container capped at 12.

Everything is derived afterwards from those files. Nothing is accumulated
in flight, so any number can be recomputed on any machine, forever:

```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/bench/<run>/ ./run --profile <p>
python3 aws_pdf/bench/report.py ./run                       # PDF, one run (all reps, both arms)
python3 aws_video/bench/report.py ./run/rr                  # video, one arm
python3 aws_video/bench/report.py ./rep1 ./rep2 ./rep3      # video, reps of one arm (adds determinism)
python3 aws_video/bench/report.py --arms ./run/rr ./run/lg  # video, cross-arm gates
```

The report tooling is standard-library Python 3.10 or newer.

## 2. PDF suite (M0 to M7)

| id | metric | formula or rule | module |
|---|---|---|---|
| M0 | correctness gate | census (offered == records, no duplicates, missing docs named), structure (384-d, finite, L2 norm within 1e-3 of 1, hash count == chunk count, identity proven), determinism (ordered chunk hashes identical across reps or modes), self-duplication (repeat factor exactly 1), ground truth where a reference exists, parity fixture | `m0_correctness.py` |
| M1 | throughput | successful completions in window / window span. Warm-up completions excluded. Failed docs never count | `m1_m2_perf.py` |
| M2 | latency | completion - submit per doc, p50/p90/p95/p99/max over successes. **Labelled by mode**: closed loop = service latency; blast or batch = batch-position latency | `m1_m2_perf.py` |
| M3 | LLM tokens / cost | zero by construction (no LLM in the pipe); reserved | none |
| M4 | blast radius | collateral unrelated failures per injected fault, time to next success | `m4_m5_faults.py` |
| M5 | fault isolation | error surfaced by the server, service continued, restart required, resources recovered | `m4_m5_faults.py` |
| M6 | lines of code | non-blank non-comment lines in four layers (pipeline definition, compute, serving, client) per arm, never summed | `m6_loc.py` |
| M7 | resources | effective cores = delta cpu seconds / span; RSS peak, median, growth; threads; cpu seconds per doc and per chunk; utilisation against the arm's allocation and against the host | `m7_resources.py` |
| | provenance | the required field list; a missing field makes the run unpublishable | `provenance.py` |

Cross-arm checks: chunk ratio per document (hard 0.4 to 2.5, warn 0.8 to
1.25), byte parity of ordered chunk hashes (reported; required for
`comparison_eligible`).

The verdicts in `report.txt`:

- `RUN PASS` / `RUN FAIL`: execution, per-arm M0 and provenance. It does not
  require cross-arm byte parity.
- `comparison_eligible`: both arms present, their gates green, cross-arm
  checks green, provenance complete, no profiler capture. Use this field to
  decide whether the numbers support an equal-work comparison.

## 3. Video suite (V0 to V5)

| id | metric | rule or formula |
|---|---|---|
| V0 | gates, per arm | census, structure (as PDF, plus chunk length), frame_law (frames == floor(duration / 15) + 1, tolerance 1), self_duplication, corpus_pin (input sha256 == manifest), determinism (single rep = fail by design), metric_coverage (a null metric must be a named exemption), task_census / worker_census in matched postures |
| V0 | gates, cross-arm | input_identity, frame_parity (exact), detection_ratio (warn outside 0.90 to 1.10), chunk_ratio (hard 0.4 to 2.5, warn 0.8 to 1.25), chunk_parity_tight (warn), workload_ratio (informational) |
| V1 | throughput | **x_realtime** = footage seconds / span (headline); frames/s; chunks/s beside videos/s; sustainable realtime streams |
| V2 | latency | seq: service latency and latency per footage minute; c<N>: latency at offered concurrency; blast: batch span, completion curve, time to first result with its basis string. Never cross-compared between modes |
| V3 | efficiency | **cpu_s_per_footage_min** (primary), cpu_s per frame / detection / chunk / video; effective cores; scaling efficiency; threads activated |
| V4 | resources | peak anon RSS; cold-to-ready; LangGraph stage split and framework overhead (RocketRide is a black box here, and that asymmetry is reported); storage amplification; driver CPU and idle-burden cores (matched) |
| V5 | cost | usd per 1000 footage hours = $/h / x_realtime x 1000 (c7i.8xlarge $1.428/h); videos per day per box |

Percentiles are nearest-rank. Anything that cannot be computed is `None`,
never 0 and never infinity.

## 4. Rules for reading and quoting

1. **`cpu_s_per_chunk` (PDF) and `cpu_s_per_footage_min` (video) are the
   soundest cross-arm numbers.** They are work-normalised and tail-robust.
2. **Never quote docs/s or videos/s alone.** A document is not a fixed unit
   of work; quote chunks/s beside it. AMI chunk mass varies five-fold by
   meeting room.
3. **Latency labels are not interchangeable.** Closed-loop runs give service
   latency; blast and `b<B>` runs give batch-position latency.
4. **Utilisation is against the arm's allocation, not the host**, and it is
   span-averaged, so one slow document drags it down. Read the distribution.
5. **`INSUFFICIENT_REPS` or `UNSTABLE` means no point value.** Three reps
   minimum for a coefficient of variation; two minimum for determinism.
6. **Single-rep runs are sizing evidence.** The video report stamps them
   `evidence_grade: SIZING`. The PDF harness accepts cross-mode or cross-cell
   determinism (`bench/cross_mode_determinism.py`) as the basis for a 1-rep
   `RUN PASS`, and records that basis.
7. **Profiled runs are diagnostic.** yappi on the LangGraph arm costs about
   1.5 % on PDF and 24 % on video; `RR_TRACE` on RocketRide needs an untraced
   control cell. Never quote a profiled run's speed as performance.
8. **Threads requested is not threads activated is not effective cores.**
   `RR_THREADS` is recorded as requested; the engine's pool is not observable
   from the client (`threads_observed: null`); effective cores come from the
   cgroup.

## 5. Record schema, in brief

PDF `per_doc.jsonl` fields the gates read: `doc`, `ok`, `reason`,
`submit_ns`, `completion_ns`, `n_chunks`, `chunk_sha256[]`, `vector_dim`,
`l2_norm_min/max`, `identity_ok`, and for LangGraph additionally
`sha_header_ok`, `vectors_finite`. Batch modes add `batch_index`,
`batch_size`. Absence of a required-true field fails structure; it never
defaults to pass.

Video `per_doc.jsonl` adds `input_sha256`, `duration_s`, `n_frames_est`,
`n_detections`, `total_chars`, `embedding_sha256`, and in matched postures
`manifest_index`, `token_index`, `project_id`, `task_pid` (RocketRide) or
`worker_pid`, `port` (LangGraph). `progress.jsonl` has one line per video as
it finished; `engine_cgroup.csv` is the sampler; `shot_meta` carries
`measurement_start_epoch_ns` / `measurement_end_epoch_ns`, which window the
CPU counters so warm-up, boot and idle windows are excluded.

## 6. Validation of the suites

The PDF suite reproduced the original gate-50 report exactly from raw
records, proved determinism on real blast-versus-sequential pairs
(RocketRide 49/49, LangGraph 50/50 byte-identical), caught the embedding
duplication defect (51 of 987 documents doubled) through `self_duplication`,
and captured the fault-surfacing asymmetry (LangGraph explicit 500/400 versus
RocketRide success-shaped empties). The video suite caught a corpus metadata
defect (AMI audio and video streams of different length) through
`frame_law` on both arms identically, which is how the manifest gained
ffprobe video durations.
