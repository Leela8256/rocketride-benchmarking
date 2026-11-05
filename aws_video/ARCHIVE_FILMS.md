# ARCHIVE_FILMS.md — the archive_films corpus: complete data plan

The long-video corpus for the video benchmark: public-domain feature
films from the Internet Archive, staged to S3 through the same pipeline
discipline as the AMI corpus. This is the definitive plan: what the
dataset is, exact sizes and hours, how it gets into S3, every check
along the way, what the 10-film smoke proved, and current status.

Status (2026-08-23): **FROZEN.** The JIT stager ran overnight
(2026-08-23 00:00→03:17 PT, one pass, zero interventions): 500 films
accepted from the first 636+ queue candidates, **677.2 h of footage,
281.4 GB** in `s3://…/corpus/archive_films_v2/`, every object
byte-verified, manifest sealed
(sha `bd0c915e…4e02a1`, cross-checked laptop-side), nested subsets
10 ⊂ 100 ⊂ 500 committed. 494/500 have audio (informational — six
silent prints accepted by design). All 500 licenses explicit:
471 public-domain marks/PD/CC0, 29 CC-BY variants (exact URL per film
in the manifest). The full-collection census continues separately as
EDA. §4 describes the pipeline that produced this.

---

## 1. The dataset

**Internet Archive `feature_films` collection** —
https://archive.org/details/feature_films

| property | value |
|---|---|
| collection size | 28,468 entries (few dozen are subcollections; rest are films) |
| license | public domain / CC — no account, no token, plain HTTP |
| duration profile | **86% ≥ 60 min**, median 82 min, p25 70 / p75 99 (probed sample) |
| eligible 1-hour-plus pool | thousands (exact count lands with the census) |
| what we take per film | the modern **h.264 MP4** derivative (~0.4 GB/h; `MPEG4` fallback) |
| audio | mostly talkies; silent prints filtered twice (census + staging gate) |

### The target corpus (N=500)

| quantity | value | basis |
|---|---|---|
| films | 500 = the first queue candidates to pass every gate | deterministic queue (downloads desc, identifier asc) |
| footage | **≈ 700 hours** (median ~82 min/film) | duration profile |
| bytes in S3 | **≈ 300 GB** | ~0.4 GB/h × derivative sizes (10-film smoke: 5.5 GB for 13.3 h — extrapolates to ~290 GB) |
| S3 cost | **≈ $7/month** | 300 GB × $0.023 |
| staging wall time | **~10–16 h (one overnight)** | archive.org at 5–20 MB/s, sequential |
| box cost to stage | ~$15–25 | c7i.8xlarge $1.43/h |

### Acceptance gates (JIT, applied per candidate in queue order)

1. item metadata resolves with files (dark items rejected)
2. **explicit license allowlist** — CC family + public-domain marks
   (`creativecommons.org/` `publicdomain/mark`, `publicdomain/zero`,
   `licenses/publicdomain`, `licenses/by*`); an item with NO license
   metadata is **rejected, never assumed PD**
3. source-reported duration **60–240 min** (1-hour-plus requirement; cap
   keeps a 10-hour outlier from distorting run sizing)
4. a **deterministic MP4 derivative** pick exists (h.264 > MPEG4 > 512Kb
   MPEG4; ties broken by longer length, then larger size, then name)
5. **dedup vs already-accepted**: normalized title AND duration within
   max(120 s, 2%) — re-uploads collapse, remakes with different runtimes
   survive
6. post-download: **probe-corroborated duration** — ffmpeg-probed video
   stream within max(300 s, 10%) of source-reported (catches broken
   metadata and truncated downloads)
7. **no audio requirement** — the benchmark pipe is detect-only; audio
   presence is recorded per film, informational only

## 2. What the 10-film smoke proved (run `archive10-20260822T190711Z`)

- **The pipeline works on this data**: `.mp4` routes into RocketRide's
  webhook video lane (previously only proven for `.avi`); both arms
  processed 8/8 films (63–105 min, incl. a 1080p print) with valid
  structure; `corpus_pin` verified all shas.
- **Staging works**: 10/10 downloaded (no retries needed), 0 audio-gate
  skips, probed, sha'd, manifest complete — ~30 min for 5.5 GB.
- **The one real finding — VFR frame divergence**: old prints carry
  variable/odd frame timestamps; RocketRide's reader over-samples them
  by up to +6% vs the video clock while ffmpeg (LangGraph) samples
  exactly. Detections and chunks stayed inside all equivalence bands
  (workload ratio 1.012). **Fixed in the gates** (see §5) and
  **instrumented in the stager** (per-film fps + counted frames in the
  manifest) so VFR-suspicious prints are identifiable.
- Performance notes: long films are RocketRide's best case (41.9× — blast
  amortizes per-doc overhead); LangGraph's decode share doubles (~17%)
  on high-res prints.

## 3. The corpus contract

One corpus doc = one film = `<identifier>.mp4` in the **provisional
versioned prefix**
`s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/`
(the 10-film smoke set stays untouched at `corpus/archive_films/`).

**The queue is the selection order; the freeze is the corpus definition.**
Committed to git:
- `corpus/sets/archive_films_queue.txt` — top 1,500 by (downloads desc,
  identifier asc), downloads snapshot in the header
- after the freeze: **nested subsets** `archive_films_10.txt` ⊂
  `archive_films_100.txt` ⊂ `archive_films_500.txt` (prefixes of
  acceptance order — every run scale uses a subset of the same corpus)
  + `archive_films_500_durations.json`

**The journal** (`~/stage_films_v2_journal.jsonl` on the box) records
EVERY decision — one line per candidate, accepted or rejected with
reason — and is the resume state.

**The frozen manifest** (`corpus_manifest.json`, sealed by its own sha in
`corpus_manifest.sha256` beside it) carries, per film:

| field | source | feeds |
|---|---|---|
| `duration_s` | source metadata | the footage denominator (x_realtime, cpu_s_per_footage_min) |
| `video_duration_s` | ffmpeg null-mux probe | the frame_law denominator (A/V and container clocks disagree — the AMI lesson) |
| `sha256` + `bytes` | staging, before upload | the corpus_pin gate; content identity forever |
| `frames_counted` + `nominal_fps` | same probe pass | VFR diagnostics (the films lesson) |
| `license` | item metadata | per-film provenance (explicit allowlist) |
| `has_audio` | probe, informational | dual-lane optionality; no gate |

## 4. The staging pipeline (JIT, census-independent)

`run/stage_films_jit.sh` → `stage_films_jit.py` + `freeze_films.py`, ON
THE BOX (direct archive.org → box → S3; nothing routes through the
laptop). No pre-built film list: the stager walks the committed queue and
decides each candidate just-in-time.

```
for each identifier in archive_films_queue.txt (downloads desc, id asc):
  stop     when 500 accepted
  skip     if already journaled (resume state = the journal itself)
  fetch    item metadata just-in-time (archive.org /metadata)
  gate     license allowlist → duration 60–240 min → deterministic mp4
           pick → title+duration dedup            (rejects cost ~1 s, no bytes)
  download curl from archive.org (302 → datanode, retries, ~5–20 MB/s,
           SEQUENTIAL — politeness)
  probe    one ffmpeg null-mux pass → video_duration_s + frames_counted +
           nominal_fps + has_audio(informational)
  gate     probed duration corroborates source-reported (10% / 300 s)
  record   sha256 + bytes BEFORE upload → journal
  upload   aws s3 cp → provisional prefix; byte size verified via head-object
  clean    delete local copy                     (peak disk = ONE film, <8 GB)
then freeze (freeze_films.py, automatic at 500):
  verify   EVERY S3 object against journaled bytes (head-object, no re-download)
  seal     manifest → S3 + its own sha256 beside it
  pin      nested subsets 10 ⊂ 100 ⊂ 500 + durations → corpus/sets/ + S3
           (fetched to the laptop and committed — the commit is the freeze)
```

If the 1,500-queue exhausts before 500 accepted (exit 3), extend it:
`make_films_queue.py --q 3000` and relaunch — journaled candidates are
skipped, so the extension only appends new work.

Operational hardening (each from a real incident):
- **Keepalive**: a one-core busy loop runs for the stager's lifetime —
  the box idle-watchdog stops network-bound work otherwise (it killed
  the AMI staging mid-run).
- **Resumable at every step**: interruption of any kind costs nothing;
  relaunch continues at the first unstaged film.
- **`aws` resolved by absolute path** (nohup shells lose it).
- Peak disk ~4 GB regardless of corpus size (delete-as-you-go), so
  disk can never be the failure mode.

Launch:

```bash
bash local/box.sh start
bash local/box.sh run 'cd ~/rocketride-benchmarking && git pull --ff-only origin main'
bash local/box.sh launch stagefilms \
  'cd ~/rocketride-benchmarking/aws_video && bash run/stage_films_jit.sh'
bash local/box.sh tail stagefilms   # "ACCEPT <id>.mp4 (n/500 ...)" / "REJECT <id> (reason)"
```

(`run/stage_archive_films.sh` remains for the 10-film smoke prefix; new
staging goes through the JIT path.)

## 5. Checks — before, during, and after

**Before any video byte (per candidate, JIT):** metadata resolves;
explicit license allowlist; source duration window; deterministic mp4
pick; title+duration dedup — every rejection journaled with its reason,
so the acceptance funnel is fully auditable afterwards
(`tallies: {...}` in the log summarizes it live).

**At staging (per film):** download integrity (curl `-f` + retries);
probe sanity (decode rc, duration parses, frames counted); probed
duration corroborates metadata (10% / 300 s); sha recorded before
upload; upload byte-verified via head-object.

**At freeze (automatic at 500):** every one of the 500 S3 objects
re-verified against journaled bytes; manifest sealed with its own
sha256; nested subsets emitted. Freeze refuses to run on a shortfall or
any verification miss.

**At benchmark time (the V0 suite, with the films-driven recalibrations
of 2026-08-22 — validated against all three existing run datasets):**

| gate | behavior on this corpus |
|---|---|
| `corpus_pin` | verifies every input sha against the manifest (proved on the smoke) |
| `frame_law` | asymmetric: silent frame DROPS hard-fail; VFR over-sampling ≤10% warns (annotated); dense-content chunk bound at 3×frames+1 |
| `frame_parity` | banded: exact PASS / ≤10% relative WARN ("VFR-band") / beyond FAIL |
| everything else | unchanged: census, structure, self-dup, detection ratio 0.90–1.10, chunk ratio, tight parity, coverage, determinism (needs ≥2 reps) |

## 6. After staging: how it gets benchmarked

- Smoke shape exists: `run/archive10.sh` (RR blast default threads vs LG
  c32) — scale N as needed.
- Full runs at 500: RR blast ≈ 17 h/rep at its measured 41.9× on films;
  LG ≈ 4.7 h/rep at ~150×. Waves (`run_waves.sh`, S3→RAM slices of
  30–40 films ≈ 25 GB) if bounded scratch/checkpoints are wanted; with
  1 TB disk a straight pass also fits (500 films ≈ 300 GB corpus +
  300 GB engine scratch + fixed ≈ ~650 GB peak — inside 1 TB, but waves
  give checkpoints for a 17-hour arm).
- Role in the study: the **long-video corpus** beside AMI's matched
  corpus — AMI carries the strict identical-work claims (frame parity
  exact); films carry realism, `.mp4`, hour-plus documents, and scale,
  reported with the VFR band documented.

## 7. Where everything lives

| thing | location |
|---|---|
| candidate queue (the selection order) | `corpus/sets/archive_films_queue.txt` |
| queue generator | `corpus/make_films_queue.py` (`--q 1500`) |
| JIT stager + freeze | `run/stage_films_jit.sh` → `stage_films_jit.py` + `freeze_films.py` (`--dry-run N` for gate preview) |
| decision journal (resume state, box) | `~/stage_films_v2_journal.jsonl` |
| canonical corpus (provisional → frozen) | `s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/` (+ `corpus_manifest.json` + `.sha256` + `pins/`) |
| frozen pins (after freeze) | `corpus/sets/archive_films_{10,100,500}.txt` + `archive_films_500_durations.json` |
| census (EDA only, non-blocking) | `corpus/census_archive_films.jsonl` ← `census_archive_films.py` |
| 10-film smoke set / results | `corpus/archive_films/` (S3) + `run/archive10.sh` / `s3://…/videobench/archive10-20260822T190711Z/` |
| box control (laptop) | `local/box.sh` |

## 8. Known traps (all encountered or engineered against)

1. **Dark items** — metadata without files; census flags, selection skips.
2. **Subcollections in search results** — filtered by `mediatype:movies`.
3. **Silent films** — no longer gated: the benchmark pipe is detect-only,
   so audio is not required; presence recorded per film (informational)
   to keep dual-lane optionality.
4. **Re-uploads/duplicates** — normalized title + duration proximity
   (max(120 s, 2%)); remakes with different runtimes deliberately kept.
5. **VFR/odd timestamps in old prints** — the measured +≤6% RR
   over-sampling; gates banded, fps probe recorded per film (§5).
6. **Politeness to archive.org** — sequential downloads, retries with
   delay; never fan out.
7. **Idle watchdog vs network-bound staging** — keepalive core (§4);
   nohup-detach always; resume makes any stop free.
8. **`aws` PATH in nohup shells** — absolute path in every script.
9. **Licensing** — per-item EXPLICIT allowlist (CC family + PD marks);
   items without license metadata are rejected, never assumed PD; the
   exact license URL is journaled and frozen into the manifest per film.

## 9. Timeline to a benchmark-ready 500-film corpus

| phase | duration | where |
|---|---|---|
| queue generation + commit | ~5 min (done) | laptop, one scrape pass |
| JIT staging until 500 accepted | ~10–16 h, one overnight | box (keepalive on, journal-resumable) |
| freeze: verify 500 objects + seal manifest + subsets | ~5 min, automatic | box |
| fetch pins + commit (the freeze lands in git) | minutes | laptop |
| **total** | **one overnight, unattended** | |

The census no longer appears in this table — it runs in parallel for EDA
and stratification analysis, and nothing in staging waits on it.
