# RESULTS_FILMS.md — the archive_films_v2 campaign

Every run on the 500-film Internet Archive corpus: what was configured, what the
corpus was, what came out, and what each phase settled. Newest phase last.

**Every cell below is a single repetition on an unpinned 32-core box — SIZING
evidence, not a publishable comparison.** Where a number is likely distorted by
page-cache state or corpus size, it says so on the row.

Raw records for every run: `s3://rocketride-benchmark-data/leela/videobench/<run-id>/`
(per-doc records, cgroup samplers, process census, provenance, report.txt).
Re-derive any figure with `python3 bench/report.py <run>/rr` or `--arms <rr> <lg>`.

---

## 1. The corpus

| | |
|---|---|
| Source | `s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/` — 500 public-domain feature films from the Internet Archive, frozen 2026-08-23 |
| Size | 262.1 GiB, 677.2 footage-hours, 500 objects + manifest + seal |
| Seal | `corpus_manifest.sha256` = `bd0c915e28710322bace0549d7372dddea5578895333f143c67e04252e4e02a1` — the pin header quotes the same sha; verified on the box and independently by Shashi |
| Format | 1080p-class `.mp4`, h.264 derivatives of vintage prints. AMI for contrast is 720x576 DivX in AVI |
| Per document | ~526 MB, ~89 min, ~361 frames at one frame per 15 s (AMI: ~74 MB, ~35 min, 137 frames) |
| Subsets | nested in acceptance order: 10 in 50 in 100 in 500. Pins in `corpus/sets/archive_films_{10,50,100,500}.txt` |
| Licences | 490 PD / CC0 / BY / BY-SA + **10 NC-family**, classified per film in the manifest |
| Durations | ffprobe **video-stream** seconds, never container or audio length |

**Naming trap.** The pin's second column is the *original archive.org filename* and
differs from the staged filename for 213 of the 500. **Column 1 (the archive id)
plus `.mp4` is the manifest key** and resolves 100% on every subset. Anything that
trusts column 2 aborts on the first film.

**Frame parity is approximate here, unlike AMI.** Old prints have variable frame
timing, so the engine's reader over-samples: 361.1 frames/film against LangGraph's
355.3 (~1.6%). Films are "equal work within ~1.5%", never "identical" — the
frame-exact claims belong to AMI.

---

## 2. The pipeline (identical on both arms, proven per run)

```
video -> frame grabber, one frame per 15 s -> RF-DETR base, threshold 0.3
      -> character splitter 4000 -> multi-qa-MiniLM-L6-cos-v1 (384-d) -> documents
```

Gates run fail-closed before any number is printed: input SHAs against the sealed
manifest, census, structure (384-d finite normalised), frame law, self-duplication,
corpus pin, and a process census (RocketRide: one new task process per `use()` with
the six BLAS/OMP variables read back from `/proc/<pid>/environ`; LangGraph: every
worker PID with torch threads, interop and detect-concurrency read back from `/meta`).

---

## 3. Phase 1 — default postures at three scales (22-25 Aug)

**Config.** RocketRide: one `use()`, no `threads` argument, BLAS/OMP variables unset,
blast ingestion. LangGraph: one uvicorn process, 32 request threads on one shared
model, client c32. This is what each framework does with nothing configured.

| date | run | films | RR xRT | RR cores | RR $/1k fh | LG xRT | LG cores | LG $/1k fh |
|---|---|---|---|---|---|---|---|---|
| 22 Aug | `archive10-20260822T190711Z` | 8 | smoke | | | smoke | | |
| 23 Aug | `films10-20260823T170514Z` | 8 (11.5 h) | 36.4 | 5.95 | $39.28 | 129.5 | 18.94 | $11.03 |
| 23 Aug | `films50-20260823T175555Z` | 48 (71.6 h) | 37.0 | 6.25 | $38.59 | 135.3 | 24.0 | $10.55 |
| 25 Aug | `films500-20260825T061529Z` | 498 (674.8 h) | 35.0 | 6.88 | $40.79 | 154.6 | 25.46 | $9.24 |

Reruns: `films10-20260824T030826Z`, `films10-20260825T060011Z`,
`films500-20260824T034901Z` (LangGraph OOM at 97 films),
`films500-20260824T073256Z`.

**Settled.** RocketRide's one-task ceiling is **35-37x on 6-7 cores regardless of
corpus size** — 8 films and 498 films give the same answer. The `.mp4` path, vintage
codecs and 2.6x AMI's chunk mass all work.

**The OOM and its fix.** The first 498-film LangGraph attempt died at 97 films: the
arm decoded every frame of a film into memory before detecting (~2.3 GB per 90-minute
1080p print, 32 in flight) and the host killed it. It now streams frames from disk;
the rerun held resident memory flat at 9-14 GB for 4.4 hours.

---

## 4. Phase 2 — best configs, and the discovery that they differ by corpus (3 Sep)

### 4a. AMI's optimum, applied to films — `filmsbest-50-20260903T040810Z`

**Config.** RocketRide 32 tasks x OMP 1 (`threads=1`, inflight 1, 32 box-wide);
LangGraph 4 processes x c48 x torch 1. Both were the measured AMI optimum.
48 measured + 2 warm.

| arm | frames/s | xRT | cores | CPU-s/frame | peak mem | idle cores |
|---|---|---|---|---|---|---|
| RR 32x1 | 8.933 | 131.8 | 23.51 | 2.632 | 58.9 GB *(at the limit)* | 8.55 |
| **LG 4xc48** | **10.105** | 151.6 | 21.52 | **2.129** | 46.3 GB | **0.27** |

Cross-arm: detection ratio PASS on all 48, chunk ratio PASS, workload ratio 1.019.

**LangGraph 13% ahead** — the reverse of AMI, where RocketRide led by 10.2%.

### 4b. RocketRide posture sweep on films50 — `rrfilms-*-20260903T202644Z`

**Config.** RocketRide only, in-flight held at **32 box-wide in every cell**
(`tasks x inflight = 32`), so the only variable is the tasks/threads split.

| posture | frames/s | xRT | cores | CPU-s/frame | peak mem | idle |
|---|---|---|---|---|---|---|
| 32 x OMP 1 *(AMI optimum)* | 8.933 | 131.8 | 23.51 | 2.632 | 58.9 GB | 8.55 |
| **16 x OMP 2** | **10.411** | 153.6 | 25.40 | **2.44** | 47.8 GB | 5.22 |
| 8 x OMP 4 | 9.204 | 135.8 | 27.80 | 3.02 | 41.6 GB | 3.90 |

**Settled: the optimal posture is corpus-dependent and non-monotonic.** 16x2 beats
32x1 by **16.5%**, and 8x4 is worse again despite holding *more* cores (27.8) —
CPU-per-frame rises to 3.02 because intra-op scaling is sub-linear, so the extra
width is wasted. There is a genuine peak in the middle and it tracks the decode
share: AMI (4% decode) wants 32 lanes, films (~15%) want 16.

Independently confirmed: Shashi's harness measured 16x2 at 12.52 f/s against 32x1's
10.26 — the same ordering, +22% against our +16.5%.

**Caveat: these are probably cache-cold.** Shashi measured 18% between byte-identical
reps purely from page-cache state (11% iowait, gp3 at its 125 MB/s ceiling). Our
sweep ran 32x1 first — the cell that evicts the corpus — so 16x2's 10.411 is likely
understated ~15%. A prewarm was added afterwards.

### 4c. `filmsbest-500-20260903T040810Z` — partial

Stopped by request at ~40 of 498 films. No usable numbers.

---

## 5. Phase 3 — films100, both arms tuned (5 Sep)

**Why 98 films.** At c48 against 48 films LangGraph gets a **single wave**: every film
starts at once, the span is set by the longest print, and it converted only 21.5 of 32
cores. No configuration fixes that. 98 films gives LangGraph ~2 waves and RocketRide
16x2 ~6. Each cell ran in its own invocation preceded by a **page-cache prewarm**, so
cell ordering is not a hidden variable.

`films100-*-20260905T023331Z` — 98 measured + 2 warm, 145.08 footage-hours.

| cell | config | frames/s | xRT | cores | CPU-s/frame | peak mem | idle |
|---|---|---|---|---|---|---|---|
| **lg8x48t1** | LG 8 proc x c48 x torch 1 | **13.253** | 198.8 | 25.94 | 1.958 | 53.6 GB | **0.1** |
| lg4x48t1 | LG 4 proc x c48 x torch 1 | 13.049 | 195.7 | 24.92 | **1.910** | **39.4 GB** | 0.3 |
| rr16x2 | RR 16 tasks x OMP 2 | 11.103 | 163.9 | 26.95 | 2.427 | 56.8 GB | 5.38 |
| lg4x48t2 | LG 4 proc x c48 x **torch 2** | 10.097 | 151.5 | 28.59 | 2.831 | 53.7 GB | 0.9 |

All four PASS. LangGraph's concurrency was held at 48 in all three of its cells
(4x12 and 8x6 both = 48), so only process count and torch threads varied.

**Settled.**

1. **LangGraph wins on films by 19.4%**, and on efficiency too (1.958 vs 2.427
   CPU-s/frame), with 0.1 idle cores against 5.38.
2. **The wave hypothesis was right.** LangGraph's utilisation was the whole story:
   21.52 cores on films50 -> 24.9-25.9 on films100. That alone turned a 3% deficit
   into a 19% lead.
3. **RocketRide's "wider lanes" trick does not transfer.** `torch 2` cost LangGraph
   23% while holding *more* cores — sub-linear intra-op scaling again. torch=1 is
   right for LangGraph on any corpus.
4. **Processes still saturate for LangGraph**: 8 vs 4 is +1.6%, inside noise.

---

## 6. Where films and AMI disagree, and why

| | AMI, 168 (4% decode) | films100, 98 (~15% decode) |
|---|---|---|
| RocketRide best | 32x1 = **16.213 f/s** | 16x2 = 11.103 |
| LangGraph best | 4xc48 = 14.712 | 8xc48 = **13.253 f/s** |
| winner | RR **+10.2%** | LG **+19.4%** |

Throughput is `cores held / CPU-s per frame`. Splitting the two corpora that way
isolates the cause:

| | cores | CPU-s/frame |
|---|---|---|
| AMI RR -> films RR | 28.74 -> 26.95 (-6%) | 1.773 -> 2.427 (**+37%**) |
| AMI LG -> films LG | 28.68 -> 25.94 (-10%) | 1.949 -> 1.958 (**+0.5%**) |

**LangGraph's cost per frame is flat between corpora. RocketRide's rises 37%.** Since
detection is the same work on both arms (same model, same 560 px input — LangGraph
proves it costs the same), the difference is in frame extraction. Subtracting
LangGraph's measured detection cost out:

| decode cost per frame | AMI | films |
|---|---|---|
| LangGraph (measured from its stage split) | 0.078 | 0.294 |
| RocketRide (inferred) | ~0.02 | **~0.82** |

RocketRide's extraction is *cheaper* than LangGraph's on AMI and ~2.8x more expensive
on films.

**Leading hypothesis, from engine source.** Both arms hand ffmpeg the same
`fps=1/15` filter, so it is not a decode-strategy difference. But
`ai/common/avi/reader.py` can only stream from stdin for a few container formats —
`mp4/mov -> False`, `avi -> False` — so the engine **writes every file to a temp cache
and re-reads it** before decoding. Harmless at AMI's 74 MB per document; on films it
is 526 MB, or **2.7x more spool I/O per extracted frame**, against a gp3 volume capped
at 125 MB/s with 16 tasks doing it at once. Shashi measured 11% iowait and six
processes blocked on I/O on the same box and put it plainly: *"the cores were not
slower; they were spinning."* OMP spin-wait converts I/O stalls into CPU-seconds that
produce no frames.

**Unproven.** The decisive test is cheap: copy ~15 films to `/dev/shm` and run the
same cell from RAM. Same files, same code, only the I/O path differs. If the RAM run
is much faster it is I/O and the spool is the culprit; if identical, the cost is
genuinely CPU-bound decode. A decode-only pipe (`webhook -> frame_grabber -> sink`)
would measure extraction directly instead of by subtraction.

---

## 7. Open gaps

1. **RocketRide has only one posture on films100.** Its 16x2 optimum came from
   films50, where 32x1 was crippled by 1.5 waves; on films100 that same 32x1 gets
   3.06 waves and most of the handicap disappears. RocketRide is currently the
   under-tuned arm on the corpus we would quote. **One cell, ~70 min.**
2. **No films500 at best config.** The only full-corpus films data is default posture.
3. **Everything is single-rep.** Turning the headline into a quotable number needs
   3 reps per arm under a pinned cpuset.
4. **Detection parity on films is ~1-3%, not exact.** Shashi's hypothesis, unverified:
   swscale defaults to BT.709 for HD material while BT.601 agrees only at SD — which
   is where AMI's small frames live.

---

## 8. Run index

| run id | date | films | config |
|---|---|---|---|
| `archive10-20260822T190711Z` | 22 Aug | 8 | default, both arms (.mp4 shakedown) |
| `films10-20260823T170514Z` | 23 Aug | 8 | default |
| `films50-20260823T175555Z` | 23 Aug | 48 | default |
| `films500-20260824T034901Z` | 24 Aug | 498 | default — LG OOM at 97 |
| `films10-20260824T030826Z` | 24 Aug | 8 | default, rerun |
| `films500-20260824T073256Z` | 24 Aug | 498 | default, retry |
| `films10-20260825T060011Z` | 25 Aug | 8 | default, rerun |
| `films500-20260825T061529Z` | 25 Aug | 498 | default — the good full run |
| `filmsbest-50-20260903T040810Z` | 3 Sep | 48 | RR 32x1 + LG 4xc48xt1 |
| `filmsbest-500-20260903T040810Z` | 3 Sep | 498 | partial, stopped |
| `rrfilms-16x2-20260903T202644Z` | 3 Sep | 48 | RR 16 x OMP 2 |
| `rrfilms-8x4-20260903T202644Z` | 3 Sep | 48 | RR 8 x OMP 4 |
| `films100-rr16x2-20260905T023331Z` | 5 Sep | 98 | RR 16 x OMP 2 |
| `films100-lg4x48t1-20260905T023331Z` | 5 Sep | 98 | LG 4 proc x c48 x torch 1 |
| `films100-lg8x48t1-20260905T023331Z` | 5 Sep | 98 | LG 8 proc x c48 x torch 1 |
| `films100-lg4x48t2-20260905T023331Z` | 5 Sep | 98 | LG 4 proc x c48 x torch 2 |

## 9. Related

- [`ARCHIVE_FILMS.md`](ARCHIVE_FILMS.md) — how the corpus was selected, staged, licensed and sealed
- [`ARCHIVE_FILMS_S3_AND_RUNBOOK.md`](ARCHIVE_FILMS_S3_AND_RUNBOOK.md) — S3 locations and how to run it
- [`RESULTS_AMI_POSTURES.md`](RESULTS_AMI_POSTURES.md) — the AMI posture campaign this is compared against
- [`METRICS.md`](METRICS.md) — what each gate and metric means
