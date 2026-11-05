# METRICS.md — the video benchmark metric suite (RocketRide vs LangGraph)

The measurement contract for the video workload. Layered like the PDF
bench's suite (aws_pdf/METRICS_SUITE.md): gates first, numbers second —
**a run that fails a V0 gate produces no quotable numbers.** Prefixed V*
to avoid colliding with the PDF suite's M* names.

Grounded in four bodies of prior art, borrowed deliberately:
- **MLPerf Inference**: metrics are reported per submission scenario
  (offline / server / single-stream) and never blended across scenarios.
  Our seq / c\<N\> / blast modes follow the same discipline.
- **ASR benchmarking** (faster-whisper, whisper.cpp): everything time-media
  normalizes to the real-time factor. Our headline is its inverse.
- **Video-analytics systems** (NVIDIA DeepStream, Scanner-class work):
  frames/s, sustainable streams per device, per-component latency under
  scale, utilization-as-bottleneck-diagnosis.
- **Agent-framework studies**: framework overhead vs non-framework
  baseline ("framework tax").

All values in V1–V5 are derivable from what the harness already records
(per_doc.jsonl, progress.jsonl, cgroup sampler, corpus manifest) except
per-video detection counts, which need a one-line driver addition.

---

## V0 — Validity gates (pass/fail, fail-closed)

Per arm:

| gate | rule | catches |
|---|---|---|
| `census` | videos offered == records returned; attribution by filename; missing docs NAMED against the manifest; no duplicates | loss |
| `structure` | ≥1 chunk per video; every vector 384-dim, finite, L2 norm within 1e-3 of 1.0; every chunk ≤ 4000 chars | corruption |
| `frame_law` | per video: n_frames == ⌊duration_s / 15⌋ + 1, tolerance ±1 | silent frame drops (video-specific; both arms measured exact on ES2016d: 102) |
| `self_duplication` | repeat_factor exactly 1 from ordered chunk hashes | the RR embedding-flush bug class; needs no other arm, survives upgrades |
| `determinism` | ordered chunk hashes identical across reps **on the same platform** | instability. Detect pipe passes hard (RR 60/60 across three runs; LG SHA-identical reps). Dual-lane (Whisper) carries the known R5 caveat — decide patch-vs-soft-gate before dual-lane reps count |

Across arms — replaces byte parity (decision 2026-08-20: functional
replication, not byte-matched outputs):

| gate | rule |
|---|---|
| `frame_parity` | per-video frame counts match exactly (hard) |
| `detection_ratio` | per-video detection-count ratio; **warn** outside 0.90–1.10 (smoke measured 332 vs 328 = 1.012) |
| `chunk_ratio` | per-video chunk ratio; **hard** 0.4–2.5, **warn** 0.8–1.25 (inherited from the PDF suite) |
| `label_overlap` | Jaccard similarity of per-video label sets; reported, not gated |

Single-rep runs cannot pass `determinism` and are sizing/scale runs by
definition — label them so.

## V1 — Throughput (report per mode; never blend modes)

| metric | definition | note |
|---|---|---|
| **x_realtime** (headline) | footage-hours processed ÷ wall-hours | media-native; converts directly to "N hours takes X". Measured refs: RR blast 36.2–40.7x (engine default / threads=32) |
| `frames_per_s` | total frames ÷ span | density-independent visual work unit |
| `chunks_per_s` **beside** `videos_per_s` | the PDF rule: a video is not a fixed work unit (measured 15–205 chunks/video, room-dependent) | never quote videos/s alone |
| `realtime_streams` | aggregate x_realtime, i.e. how many live feeds the system could sustain | DeepStream's "streams per device" analog; the most operator-legible capacity number |

## V2 — Latency (labels are not interchangeable)

| mode | metric |
|---|---|
| `seq` | true service latency p50/p90/p99 per video, plus latency-per-footage-minute (normalizes 8-min vs 35-min docs) |
| `c<N>` | latency at fixed offered concurrency + achieved completions/s |
| `blast` | batch span (exact), per-video completion curve from client-observed events, time-to-first-result. **No per-doc service-latency claims** — batch position includes queue wait |

## V3 — Efficiency (work per resource; the soundest cross-arm numbers)

| metric | definition |
|---|---|
| **cpu_s_per_footage_min** (primary) | arm CPU-seconds ÷ footage minutes. The video analog of the PDF's cpu_s_per_chunk; robust to the ~5x chunk-density variance across AMI rooms |
| `cpu_s_per_frame`, `cpu_s_per_detection` | component-level efficiency; frames are density-independent |
| `cpu_s_per_chunk` | kept for continuity with PDF results |
| `effective_cores` + `scaling_efficiency` | measured cores (cgroup Δusage/Δt) ÷ allocated cores; plus the cores-vs-K-engines curve. Formalizes the finding that RR video holds ~5.5–5.9 of 32 regardless of threads (5.85 default, 5.59 threads=32) |

Utilization is against the ARM'S ALLOCATION, not the host (PDF rule), and
is span-averaged — read the distribution, not just the mean.

## V4 — Resources & operability (measured outcomes, not caps)

| metric | definition |
|---|---|
| `peak_rss` | per arm; memory granted equal (uncapped), memory used reported. RR measured ~7.5 GB at 60-video blast |
| **storage_amplification** | engine-side bytes written ÷ input bytes, and when they release. Measured: RR ≈ 1.0x retained for the container lifetime (survives terminate; released only at container removal); LG ≈ 0 (tempfile per request, deleted). No published benchmark tracks this; the 2026-08-19 ENOSPC run proves it decision-relevant |
| `cold_to_ready_s` | model load + warm gate; excluded from measured spans, reported as ops cost |
| `framework_overhead` (LG diagnostic) | e2e wall − Σ node timings. RR is a black box here — that asymmetry is itself reported. LG stage split also reported as workload decomposition (box smoke: decode ~55%, detect ~40%, embed ~5%) |

## V5 — Cost & capacity (price/performance)

| metric | definition |
|---|---|
| `usd_per_1k_footage_hours` | instance $/h ÷ x_realtime × 1000 |
| `videos_per_day_per_box` | at each configuration (single engine, K engines) |

## Deliberately excluded

- **WER / mAP quality scores** — both arms run the same models with the
  same thresholds; output quality is controlled by construction, not a
  variable under test.
- **Byte parity of chunks** — replaced by the V0 cross-arm bands (user
  decision). The splitter params and JSON format are still matched
  (recovered byte-exactly from engine output), so divergence measures
  model/decode differences, not formatting noise.
- **tokens/s** — no LLM in the pipe.
- **Energy** — no clean counter access on virtualized EC2.

## Reporting rules (carried from the PDF bench, still binding)

1. Provenance or it didn't happen: every knob in provenance.json; an
   incomplete provenance is not publishable.
2. One deadline, one envelope, one arm at a time; the client on its own
   cores.
3. `INSUFFICIENT_REPS` / `UNSTABLE` means no point value is quoted.
4. Disclose workload heterogeneity: AMI chunk mass varies ~5x by recording
   room (ES ~3 detections/frame vs IB ~16); chunk-normalized metrics
   absorb it, per-video numbers do not.
5. Numbers from unpinned sizing runs (everything before the first matched
   run) are sizing evidence, never benchmark results.

## Incorporated from the haystack-suite implementation (2026-08-21)

Adopted from `VIDEO-METRICS-IMPLEMENTED.md` (the sibling benchmark's
implemented suite), keeping their global rule — **anything that cannot be
computed is None, never 0, never inf**:

- **nearest-rank percentiles** (deterministic, no interpolation); p95 added
- `chunks_per_video`, `frames_per_video`, `cpu_s_per_video`,
  `video_seconds` (denominators disclosed, not derived)
- `achieved_parallelism` (alias of effective_cores) and
  `threads_activated` (cgroup pids delta; exempt in sequential mode —
  a warm pool adds no tasks)
- `failed_items` counted, never averaged into latency
- `time_to_first_result_basis` strings — an atomic batch API's "first
  result" and a per-request pool's are honest but NOT the same quantity
- cross-mode `speedup_<mode>_over_seq` + `parallel_efficiency`
  (ratio-of-ratios; meaningful only when docs >= concurrency)
- total `workload_ratio` beside the per-video bands
- gates: `corpus_pin` (records vs manifest sha map), `chunk_parity_tight`
  (per-video |Δ|<=1, totals within 5% — WARN-level here since our arms run
  different detector builds), detect-pipe chunk upper bound
  (chunks <= 1.5x frames + 1) inside `frame_law`, embedding digests in
  `determinism` (sha256 over the ordered vector bytes), and
  **metric_coverage** — a null metric must be a named exemption, so
  "we did not check" can never read as "it passed"
- provenance blocks in shot_meta: `pipe_sha256`, pipeline_kind, interval,
  model identities, split params, expect_dim

Still not implemented (their §8 + ours): bytes-over-network per video,
transport-vs-processing split, RSS-vs-length slope, max-concurrent-before-
OOM, blast-radius/recovery, toil. Do not present the suite as covering
these.

## Prior-art sources

- MLPerf Inference rules: github.com/mlcommons/inference_policies
  (inference_rules.adoc); MLPerf Inference paper: arxiv.org/abs/1911.02549
- faster-whisper benchmarks: pypi.org/project/faster-whisper
- NVIDIA DeepStream SDK: developer.nvidia.com/deepstream-sdk
- Multi-agent framework benchmarking: arxiv.org/abs/2602.03128 and the
  LangGraph/CrewAI/AutoGen empirical studies it surveys
