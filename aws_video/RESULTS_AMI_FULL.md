# AMI Meeting Corpus — Full-Corpus Benchmark Results (Run C)

**RocketRide vs LangGraph on the complete AMI corpus: 168 meetings,
98.19 hours of footage, one overnight run per arm on identical
hardware doing verified-identical work.**

| | |
|---|---|
| run | `native170-20260822T070136Z` (Run C), 2026-08-22 overnight |
| corpus | `ami_full` — all usable AMI meetings, S3-staged, sha-pinned (168 measured + 2 warm-up; 98.19 measured hours) |
| arms | RocketRide engine 3.3.1 (documented duplication correction), **blast** batch · LangGraph detect service v1, **c32** (32 in flight) — each arm on its native ingestion path |
| workload | 1 frame / 15 s → RF-DETR (thr 0.3) → JSON per frame → 4000-char chunks → multi-qa-MiniLM-L6-cos-v1 embeddings (384-dim) |
| box | c7i.8xlarge, 32 vCPU, $1.428/h on-demand |
| status | **sizing evidence, not final numbers**: single rep (determinism gate fails closed by design) and no CPU envelope (threads unpinned). One gate-calibration item (§ Gates). The matched 3-rep enveloped campaign is the remaining step. |
| raw records | `s3://rocketride-benchmark-data/leela/videobench/native170-20260822T070136Z/` (mirrored in `results/`) |

---

## Headline

| metric | RocketRide (blast) | LangGraph (c32) | gap |
|---|---|---|---|
| wall time for the whole corpus | 2.62 h | **37.9 min** | **4.16×** |
| x_realtime | 37.43 | **155.65** | 4.16× |
| cost per 1,000 footage-hours | $38.15 | **$9.17** | 4.16× |
| 30-min videos per day per box | 1,796 | **7,471** | 4.16× |
| effective cores (of 32) | 5.98 | **26.84** (84%) | 4.5× |
| CPU per footage-minute | **9.91 s** (its best showing) | 10.66 s | ~tie — RR 7% better |
| work done (chunks, cross-arm ratio) | 14,366 | 14,027 | 1.024 — equal work |

**The one-sentence result, unchanged from runs A/B and now proven at
full corpus scale: the two arms spend the same CPU per unit of work
(within 8%), but LangGraph schedules 27 of 32 cores while RocketRide's
engine holds ~6 — and that single difference produces the entire 4.16×
gap in throughput, wall time, and cost.**

RocketRide's ceiling is remarkably stable: six runs across corpus sizes
(28→60→168 videos), modes, and staging media have landed at 36.2–37.4×
realtime with 5.4–6.0 effective cores. It spawned 4,049 threads to keep
those 6 cores busy. LangGraph's utilization *rose* with backlog depth
(19.5 → 25.6 → 26.8 cores across runs A→B→C).

## V1 — Throughput

| metric | RocketRide (blast) | LangGraph (c32) |
|---|---|---|
| span (s) | 9,444.98 | **2,271.19** |
| x_realtime | 37.43 | **155.65** |
| videos/s | 0.0178 | **0.074** |
| chunks/s | 1.521 | **6.176** |
| frames/s | 2.44 | **10.148** |
| sustainable realtime streams | 37.4 | **155.6** |
| chunks/video (work check) | 85.5 | 83.5 |
| frames/video (work check) | 137.2 | 137.2 — identical |

## V2 — Latency (mode-labeled; the two arms ran different modes — do not cross-compare)

**RocketRide, blast** (batch: no per-video service latency exists):
batch span 9,444.98 s exact; client-observed completion curve p50
5,564.6 s / p90 9,430.2 s / last 9,710.6 s; time-to-first-result
260.3 s *(basis: first completion event within the batch — not
comparable to a per-request TTFR)*.

**LangGraph, c32** (per-request): service latency p50 429.9 s / p95
707.9 s / p99 787.2 s; 12.08 s of latency per footage-minute; TTFR
18.4 s *(basis: first completed request)*; 0 failed items.

Context from run A (the only same-mode comparison, both at c6):
LangGraph won every latency row ~4×. LangGraph's own p50 rising
75 s (c6) → 430 s (c32) is queue depth buying throughput — the trade
the mode labels exist to keep honest.

## V3 — Efficiency

| metric | RocketRide | LangGraph |
|---|---|---|
| cpu_s per footage-min (primary) | **9.91** | 10.66 |
| cpu_s per video | **347.4** | 373.8 |
| cpu_s per frame | **2.532** | 2.725 |
| cpu_s per detection | **0.2962** | 0.3204 |
| effective cores (of 32) | 5.98 | **26.84** |
| scaling efficiency | 0.187 | **0.839** |
| threads activated | 4,049 | **2,932** |

Per unit of work this is RocketRide's best run on record — 9.91
cpu-s/footage-min — and it still finished 4.16× behind: efficiency was
never the problem; scheduling is.

## V4 — Resources & operability

| metric | RocketRide | LangGraph |
|---|---|---|
| peak memory (cgroup, incl. page cache) | 42.9 GB | **28.8 GB** |
| cold-start to ready | 137.7 s | **71.9 s** |
| framework overhead | not measurable (black box) | **1.35 s** total across 98 h of work |
| stage split | n/a | frames 7% / detect 92% / chunk 0% / embed 1% |

LangGraph's cost center is squarely the detector (92% under
concurrency); orchestration overhead is negligible. RocketRide exposes
no internal stage timings — that asymmetry is itself a finding.

## V5 — Cost

`usd_per_1k_footage_hours = $1.428/h ÷ x_realtime × 1000`

| | RocketRide | LangGraph |
|---|---|---|
| $ per 1,000 footage-hours | $38.15 | **$9.17** |
| 30-min videos per day per box | 1,796 | **7,471** |

Processing this entire 98-hour corpus cost ≈ $3.75 (RR) vs ≈ $0.90 (LG)
of compute. Cost is throughput's mirror; the gap is the utilization
ceiling, not computation.

## Gates

| gate | result | reading |
|---|---|---|
| census | PASS ×2 — 168/168 both arms | nothing lost, nothing unexplained |
| structure | PASS ×2 | 384-dim, finite, normalized throughout |
| self_duplication | PASS ×2 | the RR double-emit bug class: absent |
| **corpus_pin** | **PASS ×2 — first run ever with it armed** | all 168 inputs match the pinned corpus shas; input provenance now closed end-to-end |
| input_identity | PASS | both arms ate identical bytes |
| frame_parity | PASS — frame counts identical on all 168 | the strongest equal-work evidence |
| detection_ratio | PASS (all in 0.90–1.10; totals 197,062 vs 196,001) | detector-build drift ≤1% aggregate |
| chunk_ratio | PASS (all in 0.8–1.25) | equal work per video |
| workload_ratio | 1.024 | equal total work |
| chunk_parity_tight | warn: 5 EN videos, all ±2 chunks | expected with different rfdetr builds |
| frame_law | **FAIL — calibration item, both arms identically** | see below |
| determinism | FAIL by design (1 rep) | keeps this run labeled sizing evidence |
| metric_coverage | PASS ×2 | every metric non-null or exempt |

**The frame_law story is good news wearing a FAIL label.** The
A/V-duration mismatches that flagged this gate in runs A/B are **gone** —
the ffprobe video-duration fix at staging worked (ES2008c, ES2011c,
IS1000a no longer trip). What FAILs now is the gate's *chunk-upper-bound
clause* (chunks ≤ 1.5×frames+1, borrowed from the haystack suite) on 4–5
ultra-dense Idiap-room videos — IN1002 (259 chunks/165 frames), IN1007
(294/160), IN1009, IS1003c, and borderline IS1009a on RR only —
**identically on both arms**. That is a workload property of
detection-dense rooms, not an arm defect, and the bound needs raising
(~3×) or replacing with a chars-based bound. Until that recalibration,
the suite's verdict is FAIL and these numbers stay diagnostic.

## Workload character (why those rooms are dense)

Chunk mass varies **8.5×** by AMI room type — detection JSON, not
speech, drives it:

| series | videos | chunks/video | chunks/footage-hour |
|---|---|---|---|
| ES (scenario, Edinburgh) | 60 | 29.6 | 57.9 |
| EN (non-scenario) | 16 | 35.6 | 38.0 |
| TS (scenario, TNO) | 37 | 88.7 | 147.3 |
| IS (scenario, Idiap) | 38 | 133.2 | 283.6 |
| IB (non-scenario, Idiap) | 7 | 164.9 | 268.4 |
| IN (non-scenario, Idiap) | 10 | 252.7 | 309.8 |

Densest documents: IN1016 (319 chunks), IN1001 (302), IN1007 (294) —
the videos behind the frame_law calibration trip. Totals across the
corpus: 23,049 frames per arm (identical), ~197k detections, ~14k chunks.

## What this run settled, and what remains

Settled: the utilization-ceiling result holds at full corpus scale and
is insensitive to corpus size, mode, staging medium, and thread knobs;
corpus provenance (corpus_pin) and the A/V-duration fix are proven;
equal-work is established by four independent gates.

Remaining before the numbers are quotable: the frame_law bound
recalibration, then the matched campaign — shared CPU envelope
(cpuset + OMP_NUM_THREADS=1 both arms), ≥3 reps per arm/mode (arms the
determinism and CV gates), seq baselines. Expect LangGraph's margin to
compress somewhat under the envelope: unpinned, its torch threads spread
across idle cores — exactly what the envelope constrains.

---

*Generated 2026-08-23 from the run's own records (report.txt,
per_doc.jsonl ×2, manifests). Every number above is re-derivable from
`s3://rocketride-benchmark-data/leela/videobench/native170-20260822T070136Z/`.*
