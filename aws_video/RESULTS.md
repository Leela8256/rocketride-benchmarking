# Video Benchmark — Results to date (2026-08-21)

Two cross-arm runs, same videos, one engine per arm, each arm measured
as it ships. **Caveat that governs everything below:** both runs are
unpinned (no CPU envelope), single-rep — *sizing evidence*. The matched
3-rep enveloped campaign is still ahead; margins (especially LangGraph's)
may compress once intra-op threads are pinned on both arms.

Raw records (every number re-derivable forever):
- **Run A — "c6 head-to-head"**: 28 videos / 14.51 h, both arms at 6-in-flight →
  `s3://rocketride-benchmark-data/leela/videobench/h2h-20260821T195300Z/`
- **Run B — "native saturation"**: 60 videos / 31.43 h, each arm on its native
  ingestion path (RR: one blast batch; LG: all 60 requests at t=0) →
  `s3://rocketride-benchmark-data/leela/videobench/native60-20260821T210828Z/`

- **Run C — "full corpus"** (2026-08-22, overnight): all 168 usable measured
  AMI meetings + 2 warm / 98.19 h — RR blast vs LG c32, unpinned, 1 rep →
  `s3://rocketride-benchmark-data/leela/videobench/native170-20260822T070136Z/`

### Run C headline (full corpus, 98.19 h)

| | RocketRide (blast) | LangGraph (c32) | gap |
|---|---|---|---|
| span | 2.62 h | **37.9 min** | **4.16×** |
| x_realtime | 37.43 | **155.65** | 4.16× |
| effective cores | 5.98 | **26.84** (84%) | 4.5× |
| threads activated | 4,049 | 2,932 | — |
| cpu_s per footage-min | **9.91** (its best) | 10.66 | ~tie (7.5%) |
| cost per 1k footage-hours | $38.15 | **$9.17** | 4.16× |
| workload ratio | — | — | 1.024 (equal work) |
| census / frame_parity | 168/168 both | 168/168 identical | — |

Run C gate news: **corpus_pin PASSED for the first time** (168 docs vs
pinned shas, both arms); the A/V-duration frame_law failures are GONE
(the ffprobe video-duration fix worked — ES2008c/ES2011c/IS1000a no
longer flagged). One new calibration item: the chunk-upper-bound clause
(chunks ≤ 1.5×frames+1, borrowed from the haystack suite) trips on 4–5
ultra-dense IN/IS videos (IN1007: 294 chunks / 160 frames = 1.84×) —
identically on both arms, so it is a workload property of the densest
Idiap rooms, not a defect; the bound needs raising (~3×) or replacing
with a chars-based bound. determinism: FAIL by design (single rep).

---

## Scoreboard — who wins what

| # | metric | winner | margin |
|---|---|---|---|
| 1 | **Throughput** (x_realtime) | **LangGraph** | 3.8× (A) → **4.1×** (B) |
| 2 | **Cost** ($ per 1k footage-hours) | **LangGraph** | **4.1× cheaper** ($9.47 vs $39.23) |
| 3 | **Latency** (same mode, c6 — the only apples-to-apples latency) | **LangGraph** | p50 4.0× lower (75 s vs 302 s) |
| 4 | Time-to-first-result (run A, same basis) | **LangGraph** | 39 s vs 160 s |
| 5 | **CPU efficiency** (cpu-s per footage-min) | **tie** | ≤10% apart, winner alternates by run |
| 6 | Core utilization (effective cores of 32) | **LangGraph** | 25.6 vs 5.9 (B) |
| 7 | Peak memory (raw cgroup, incl. cache) | **LangGraph** | 19.9 vs 34.7 GB (B); 6.2 vs 23.7 (A) |
| 8 | Cold-start to ready | **LangGraph** | ~55 s vs ~110 s |
| 9 | Work done (equal-work check, not a race) | **tie by design** | ratio 1.013 |
| 10 | Determinism (from earlier same-platform rep pairs) | **tie** | both byte-identical across reps |

**LangGraph 6 · ties 3 · RocketRide 0** — with two fairness notes:
the c6 latency win is the only cross-arm latency comparison allowed (in
run B the modes differ by design and their latency numbers must not be
compared); and the unpinned setup lets LangGraph's torch spread across
idle cores, which the future envelope will constrain. RocketRide's one
bright spot is row 5: per unit of work it is exactly as CPU-efficient as
LangGraph (marginally better in run B) — its losses everywhere else stem
from a single cause, the ~6-core scheduling ceiling.

---

## The cost metric, up front (V5)

`usd_per_1k_footage_hours = instance $/hour ÷ x_realtime × 1000`, at the
box's on-demand price ($1.428/h, c7i.8xlarge):

| | Run A (c6) | Run B (native) |
|---|---|---|
| RocketRide | $39.17 | $39.23 |
| **LangGraph** | **$10.30** | **$9.47** |
| videos/day/box (30-min videos) | RR 1,747–1,750 | LG 6,654–**7,235** |

Read: processing 1,000 hours of meeting video costs ~$39 of compute on
RocketRide and ~$9.50 on LangGraph, on identical hardware doing
verified-identical work. Cost is throughput's mirror — the entire gap is
the utilization ceiling (row 6), not computation efficiency (row 5).

---

## Detailed tables

### V1 — Throughput

| metric | A: RR (c6) | A: LG (c6) | B: RR (blast) | B: LG (c60) | winner |
|---|---|---|---|---|---|
| x_realtime | 36.46 | 138.63 | 36.40 | **150.73** | LG |
| span (s) | 1,432.8 | 376.8 | 3,108.8 | **750.8** | LG |
| videos/s | 0.0195 | 0.0743 | 0.0193 | **0.0799** | LG |
| chunks/s | 0.731 | 2.736 | 1.754 | **7.169** | LG |
| frames/s | 2.432 | 9.249 | 2.410 | **9.981** | LG |
| realtime streams sustainable | 36.5 | 138.6 | 36.4 | **150.7** | LG |
| chunks/video (workload check) | 37.4 | 36.8 | 90.9 | 89.7 | tie (equal work) |
| frames/video | 124.5 | 124.5 | 124.9 | 124.9 | tie (identical) |

RocketRide has now run five times: four land inside 36.2–36.8×
(36.18, 36.46, 36.40, 36.76) across modes, corpus sizes, and staging
media; the threads=32 run was the one outlier at 40.66× — 12% faster
with byte-identical output, attributed to scheduling variance since its
CPU usage was unchanged. The ceiling, not the exact number, is the hard
engine property.

### V2 — Latency (mode-labeled; cross-arm comparison ONLY within run A)

| metric | A: RR (c6) | A: LG (c6) | B: RR (blast) | B: LG (c60) |
|---|---|---|---|---|
| service p50 / p95 / p99 (s) | 302 / 361 / 365 | **75 / 94 / 94** | — (batch mode) | 359 / 437 / 447 |
| latency per footage-min (s) | 9.82 | **2.36** | — | 11.48 |
| batch span (s), exact | — | — | 3,108.8 | — |
| completion curve p50/p90/last (s) | — | — | 2,110 / 3,324 / 3,343 | — |
| time_to_first_result (s) | 159.8 | **39.1** | 215.8 ⁽ᵇᵃᵗᶜʰ ᵇᵃˢⁱˢ⁾ | 172.3 ⁽ᵖᵉʳ⁻ʳᵉᑫ ᵇᵃˢⁱˢ⁾ |
| failed items | 0 | 0 | 0 | 0 |

Run A winner: LangGraph, 4× on every latency row (same mode, same basis
— a legitimate comparison). Run B: no cross-arm latency winner is
declared — blast has no per-video service latency, and the two TTFR
bases are different quantities. Note LG's own p50 rising 75→359 s from
c6 to c60: maximal throughput buys queue-depth latency, the exact
trade the mode labels exist to keep honest.

### V3 — Efficiency

| metric | A: RR | A: LG | B: RR | B: LG | winner |
|---|---|---|---|---|---|
| cpu_s per footage-min (primary) | 10.47 | **9.46** | **10.42** | 10.88 | **tie** (≤10%, alternates) |
| cpu_s per video | 325.4 | 294.1 | 327.4 | 341.9 | tie |
| cpu_s per frame | 2.615 | 2.363 | 2.622 | 2.738 | tie |
| effective cores (of 32) | 5.45 | **19.47** | 5.87 | **25.64** | LG |
| scaling efficiency | 0.17 | 0.61 | 0.18 | **0.80** | LG |
| threads activated | 998 | 225 | **3,804** | 1,960 | LG (fewer threads, more work) |

The study's central result lives in this table: identical per-unit cost,
4.4× utilization difference. RocketRide spawns 3,804 threads to keep
5.87 cores busy; LangGraph reaches 80% of the machine with half that.

### V4 — Resources & operability

| metric | A: RR | A: LG | B: RR | B: LG | winner |
|---|---|---|---|---|---|
| peak memory, raw cgroup incl. cache (GB) | 23.7 | **6.2** | 34.7 | **19.9** | LG |
| cold-start to ready (s) | 118.2 | **58.8** | 104.0 | **54.0** | LG |
| framework overhead (s, total per run) | not measurable | **0.14** | not measurable | **0.47** | — (LG's is ~zero; RR is a black box — the asymmetry is itself a finding) |
| stage split | n/a | frames 12% / detect 87% / embed 1% | n/a | frames 6% / detect 92% / embed 1% | — |

Two notes: raw peak memory includes reclaimable page cache (the
cache-corrected anon numbers ran ~4.1 GB RR / ~2.8 GB LG mid-run in A);
and under concurrency LangGraph's profile shifts to ~90% detection —
decode parallelizes away, inference is the true cost center.

## V0 — Gates (identical verdicts both runs)

| gate | result | reading |
|---|---|---|
| census | PASS ×4 (28/28, 60/60 per arm) | nothing lost, nothing unexplained |
| structure | PASS ×4 | 384-dim, finite, normalized throughout |
| self_duplication | PASS ×4 | the RR double-emit bug class: absent |
| input_identity | PASS (28, 60) | both arms ate identical bytes |
| frame_parity | **PASS — identical frame counts on every common video, both runs** | the strongest equal-work evidence |
| detection_ratio | PASS (all within 0.90–1.10) | detector-build drift ≤10% |
| chunk_ratio | PASS (all within 0.8–1.25) | equal work per video |
| workload_ratio | 1.02 (A), 1.013 (B) | equal total work |
| chunk_parity_tight | WARN: 3 videos (A) / 5 (B), all ±2–3 chunks | expected with different rfdetr builds |
| determinism | FAIL — single rep, fails closed by design | keeps these runs labeled sizing evidence |
| frame_law | FAIL on 2 (A) / 6 (B) videos — identically on BOTH arms | **corpus finding, not arm defect**: ~10% of AMI meetings have video/audio stream-length mismatch (up to ~90 s); manifest durations come from audio. Fix: ffprobe video durations at staging |
| corpus_pin | SKIP | sha map exists in the corpus manifest; drivers don't copy it into run manifests yet (two-line fix queued) |
| metric_coverage | PASS ×4 | every metric non-null or exempt |

## Findings (consolidated)

1. **Equal work, equal per-unit efficiency, 4× utilization gap.** Work
   equivalence proven by gates; CPU per footage-minute within 10%
   either way; LangGraph schedules 19.5→25.6 cores as backlog deepens,
   RocketRide holds 5.4–5.9 in every configuration tested. Throughput
   and cost gaps (3.8→4.1×) follow entirely from scheduling.
2. **RocketRide's ceiling is engine-bound and unmovable by any shipped
   knob** — threads=32 left utilization unchanged (5.59 cores; its 40.7×
   span was scheduling variance, not extra CPU); 3,804 threads for 5.87 busy
   cores at 60-video blast (see findings/rocketride_cpu_utilization.md).
3. **LangGraph's framework tax is ~zero** (0.14–0.47 s total per run) —
   consistent with the agent-framework literature; its cost center is
   inference (~90% under concurrency), not orchestration.
4. **The suite catches real things**: the AMI A/V-length corpus defect
   (twice), the memory-inflation trap (cache vs anon), and the
   threads-vs-cores gap — each surfaced by a metric added for exactly
   that purpose.

## What remains before results are quotable

Envelope (shared cpuset, OMP_NUM_THREADS=1 both arms, one deadline,
client pinned) → ≥3 reps per arm/mode → seq baselines (activates
cross-mode speedup/parallel-efficiency) → ffprobe video durations +
corpus_pin driver fix → full-corpus (170-video) campaign.
