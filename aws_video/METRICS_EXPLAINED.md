# Video Benchmark — Metrics & Correctness Reference (team status, 2026-08-21)

The complete list of what the harness computes — every gate, every
metric, its exact rule/formula, where the inputs come from, and the
latest measured values (from the first head-to-head,
`h2h-20260821T195300Z`: 28 videos / 14.51 h footage, mode c6, 32 cores
unpinned, single rep — **sizing evidence, not final benchmark numbers**).

Code: `bench/metrics/v0_gates.py` (gates), `bench/metrics/v_metrics.py`
(numbers), `bench/report.py` (runner). Spec: `METRICS.md`.
Global rule: anything uncomputable is `None` — never 0, never inf.

---

## V0 — Correctness gates (fail-closed; a FAIL makes numbers non-quotable)

### Per arm

| gate | exact rule | status on h2h |
|---|---|---|
| `census` | records == manifest docs, matched by filename; no duplicates; no missing; `identity_ok` true on all; every failure named with its reason; empty output = failure | PASS 28/28 both arms |
| `structure` | per ok video: n_chunks ≥ 1; every vector exactly 384-dim, finite; L2 norm within 1e-3 of 1.0; len(chunk_sha256) == n_chunks; mean chunk chars ≤ 4096 | PASS both arms |
| `frame_law` | n_frames_est == ⌊duration_s/15⌋+1, tolerance ±1; plus detect-pipe bound n_chunks ≤ 1.5×frames+1 | **FAIL on 2 videos, both arms identically** — see finding below |
| `self_duplication` | no duplicate chunk hashes within any video (repeat_factor == 1); needs no other arm; catches the RR 3.3.1 double-emit bug class | PASS both arms |
| `corpus_pin` | per-video input_sha256 == manifest sha256 map; SKIP if manifest has no sha map | SKIP (ami30test has no sha map; staged `ami_full` will) |
| `determinism` | ordered chunk_sha256 identical across reps on the same platform; also embedding_sha256 (sha256 over ordered vector bytes) when present; **single rep = FAIL by design** | FAIL (1 rep — expected; labels run as sizing) |
| `metric_coverage` | every V1/V2/V3/V5 field non-null OR a named exemption; "not checked" can never read as "passed" | PASS both arms |

**frame_law finding (new, 2026-08-21):** ES2008c (139 frames vs ~141
expected) and ES2011c (114 vs ~108) failed identically on BOTH arms while
`frame_parity` passed 28/28 — so the arms are correct and the **manifest
duration is wrong for those videos**: AMI's video and audio streams have
different lengths for some recordings (ES2011c's video runs ~90 s past
its audio; our durations came from the audio/WAV header). Fix queued:
record ffprobe VIDEO-stream duration in the manifest at staging time.

### Cross-arm (replaces byte parity — decision 2026-08-20)

| gate | exact rule | h2h result |
|---|---|---|
| `input_identity` | per common video: input_sha256 equal (both arms ate the same bytes) | PASS 28/28 |
| `frame_parity` | n_frames_est exactly equal per video (hard) | PASS 28/28 identical |
| `detection_ratio` | per-video n_detections ratio; WARN outside 0.90–1.10 | PASS — all inside |
| `chunk_ratio` | per-video n_chunks ratio; HARD fail outside 0.4–2.5; WARN outside 0.8–1.25 | PASS — all inside warn band |
| `chunk_parity_tight` | per-video \|Δchunks\| ≤ 1 AND totals within 5% (haystack-suite rule; WARN-level here because our arms run different rfdetr builds) | WARN on 3 videos (±2 chunks: EN2002a 26v24, ES2010c 24v22, IB4002 138v140) |
| `workload_ratio` | Σchunks(RR) ÷ Σchunks(LG), informational | ~1.02 (equal work) |

## V1 — Throughput (per mode, never blended across modes)

| metric | formula | RR (h2h) | LG (h2h) |
|---|---|---|---|
| `x_realtime` (headline) | video_seconds ÷ span_s | **36.46** | **138.63** |
| `videos_per_s` | ok docs ÷ span | 0.0195 | 0.0743 |
| `chunks_per_s` | Σ chunks ÷ span | 0.731 | 2.736 |
| `frames_per_s` | Σ frames ÷ span | 2.432 | 9.249 |
| `chunks_per_video` | Σ chunks ÷ ok docs | 37.4 | 36.8 |
| `frames_per_video` | Σ frames ÷ ok docs | 124.5 | 124.5 (identical) |
| `realtime_streams_sustainable` | = x_realtime (live feeds the box could keep up with) | 36.5 | 138.6 |
| `video_seconds` | Σ manifest durations (disclosed denominator, never probed at runtime) | 52,238.3 | 52,238.3 |
| `span_s` | measured wall span, warm-up excluded | 1,432.78 | 376.81 |

## V2 — Latency (mode-labeled; percentiles are NEAREST-RANK, deterministic)

Per-item modes (seq, c\<N\>):

| metric | formula | RR (c6) | LG (c6) |
|---|---|---|---|
| `service_latency_s` p50/p95/p99 | completion − submit per video, nearest-rank | 302.3 / 360.8 / 365.3 | 75.3 / 93.8 / 94.3 |
| `latency_s_per_footage_min` | latency ÷ (duration/60), p50 | 9.82 | 2.36 |
| `failed_items` | count of failed videos (counted, never averaged into latency) | 0 | 0 |
| `time_to_first_result_s` (+ `_basis`) | first completed request; basis string states what was measured | 159.8 | 39.1 |

Batch mode (blast) instead reports: exact `batch_span_s`, completion
curve p50/p90/last from client-observed events, TTFR with basis =
"first client-observed completion within the batch", and an explicit
refusal to claim per-doc service latency (batch position includes queue
wait).

## V3 — Efficiency (work per resource — the fairest cross-arm numbers)

| metric | formula | RR (h2h) | LG (h2h) |
|---|---|---|---|
| `cpu_s_per_footage_min` (primary) | cpu_s ÷ footage minutes | **10.47** | **9.46** |
| `cpu_s_per_video` | cpu_s ÷ ok docs | 325.4 | 294.1 |
| `cpu_s_per_frame` | cpu_s ÷ Σ frames | 2.615 | 2.363 |
| `cpu_s_per_detection` | cpu_s ÷ Σ detections | 0.566 | 0.513 |
| `cpu_s_per_chunk` | cpu_s ÷ Σ chunks (continuity with the PDF suite) | 8.704 | 7.988 |
| `effective_cores` / `achieved_parallelism` | Δcpu_usage ÷ Δt from cgroup counters | **5.45** | **19.47** |
| `threads_activated` | max(pids) − baseline pids over the span | **998** | **225** |
| `scaling_efficiency` | effective_cores ÷ allocated_cores (32) | 0.17 | 0.61 |

Reading: **per-unit CPU is nearly equal (≤10% apart on every row)** —
the arms are equally efficient per core-second. The entire 3.8×
throughput gap is utilization: 5.45 vs 19.47 busy cores. RR spawns 4×
the threads and keeps ⅓ of the cores busy — the engine's internal
serialization, quantified. (threads=32 was already proven a no-op:
5.59 cores. See `findings/rocketride_cpu_utilization.md`.)

## V4 — Resources & operability

| metric | source | notes / latest |
|---|---|---|
| peak memory (raw) | cgroup memory.current max | includes page cache — inflates ~5× |
| peak RSS (corrected) | cgroup memory.stat `anon` max | RR ~4.1 GB, LG ~2.8 GB mid-run |
| `cold_to_ready_s` | warm-up span, excluded from measurement | RR 118.2 s (2 videos); LG warm gate at boot |
| `lg_stage_split` | Σ per-node timings (LG only) | decode ~55%, detect ~40%, embed ~5% |
| `lg_framework_overhead_s` | e2e − Σ node timings (the "framework tax") | reported per run |
| RR stage split | — | not decomposable (closed engine); the asymmetry itself is reported |
| storage amplification | engine bytes written ÷ input bytes | RR ≈ 1.0× retained until container removal; LG ≈ 0 (tempfile per request) |

## V5 — Cost

| metric | formula | RR | LG |
|---|---|---|---|
| `usd_per_1k_footage_hours` | $/h ÷ x_realtime × 1000 (c7i.8xlarge $1.428/h) | **$39.17** | **$10.30** |
| `videos_per_day_per_box` | x_realtime × 48 (30-min videos) | 1,750 | 6,654 |

## Cross-mode (same arm, seq + concurrent runs available)

| metric | formula |
|---|---|
| `speedup_<mode>_over_seq` | chunks_per_s(mode) ÷ chunks_per_s(seq) — ratio of ratios, immune to unequal worker grants |
| `parallel_efficiency` | speedup ÷ offered concurrency; meaningful only when docs ≥ concurrency |

Not yet exercised (needs a seq run of each arm — queued for the matched campaign).

## Provenance recorded in every run's shot_meta

`pipe_sha256`, pipeline_kind (detect), interval_s 15, detect_model rfdetr
+ threshold 0.3, embed_model multi-qa-MiniLM-L6-cos-v1, split 4000/0,
expect_dim 384, mode, offered_concurrency, threads_requested,
warm_docs/warm_s, timeout_s, envelope string, arm id.

## Known not-implemented (do not present the suite as covering these)

Bytes-over-network per video · transport-vs-processing split ·
peak-RSS-vs-video-length slope · max-concurrent-before-OOM ·
blast-radius / recovery behavior · toil accounting. (Same gap the
haystack suite discloses in its §8.)

## Status summary

- Suite fully implemented and exercised end-to-end on real cross-arm
  data (h2h-20260821T195300Z). All incorporations from
  `VIDEO-METRICS-IMPLEMENTED.md` landed 2026-08-21.
- Outstanding before quotable results: the matched enveloped run
  (BENCH_CPUSET + OMP=1 + one deadline), ≥3 reps per arm/mode, seq runs
  for the cross-mode block, ffprobe video-durations in the staged
  manifest (fixes the frame_law corpus finding), full-corpus staging.
