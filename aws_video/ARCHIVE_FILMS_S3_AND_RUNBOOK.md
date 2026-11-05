# Archive Films corpus — S3 locations and how to run the video benchmark

Everything needed to reproduce the RocketRide-vs-LangGraph video benchmark on the
500-film Internet Archive corpus: where the data and its seal live, how to verify it,
how to smoke-test, and how to run each subset.

Bucket: `rocketride-benchmark-data` (AWS account 250017478984, `us-east-1`).
Access is the shared SSO permission set **`BenchmarkBoxOperator`**, which grants
bucket-wide read — team members already on it (Shashi, Ansh) need no extra grant.

---

## 1. The corpus

| what | S3 URI |
|---|---|
| **Films (500 objects, 262.1 GiB)** | `s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/` |
| **Manifest** (per-film sha256, bytes, probed duration, licence) | `s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/corpus_manifest.json` |
| **Seal over the manifest** | `s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/corpus_manifest.sha256` |
| Pin — 10 films | `.../archive_films_v2/pins/archive_films_10.txt` |
| Pin — 100 films | `.../archive_films_v2/pins/archive_films_100.txt` |
| Pin — 500 films | `.../archive_films_v2/pins/archive_films_500.txt` |
| Probed durations, 500 | `.../archive_films_v2/pins/archive_films_500_durations.json` |
| Earlier 10-film smoke set (superseded) | `s3://rocketride-benchmark-data/leela/corpus/archive_films/` |

**The 50-film pin is not in S3** — it lives only in the repo at
`aws_video/corpus/sets/archive_films_50.txt`. Copy it up if you want S3 to be
self-contained.

Corpus facts: 500 public-domain feature films, **677.2 h of footage**, nested subsets
10 ⊂ 50 ⊂ 100 ⊂ 500 in acceptance order. Licences: 490 PD / CC0 / BY / BY-SA and
**10 NC-family**, each classified in the manifest — check before redistributing.
Durations are **ffprobe video-stream** seconds, never container or audio length.

### Fetch just the metadata (a few hundred KB, no film bytes)

```bash
aws s3 cp --recursive --exclude "*" \
  --include "corpus_manifest.*" --include "pins/*" \
  s3://rocketride-benchmark-data/leela/corpus/archive_films_v2/ ./films_meta/ --profile leela

cd films_meta && sha256sum -c corpus_manifest.sha256   # prove the manifest is untampered
```

That chain — seal over manifest, manifest over films — is what makes a rebuilt copy
*provably* identical rather than merely similar. Any run that skips it is not comparable.

### Rebuilding without S3

The corpus is reproducible from archive.org by design. With the pin files plus
`run/stage_films_jit.sh` and `corpus/make_films_queue.py`, anyone can rebuild it and
verify against the manifest — no S3 access and no egress cost. Procedure in
[`ARCHIVE_FILMS.md`](ARCHIVE_FILMS.md).

---

## 2. Prerequisites

Linux box with Docker, ~32 vCPU, 61 GB RAM, ~400 GB free disk (500 films are 262 GiB),
and the `aws` CLI authenticated. On our AWS box: `bash local/box.sh start`.

Long runs must be detached — an SSM session will not outlive them, and the box's idle
watchdog stops the instance during low-CPU phases, so every runner starts its own
keepalive:

```bash
nohup bash run/<script>.sh > ~/logs/<name>.log 2>&1 < /dev/null &
```

---

## 3. Smoke tests — run these before any measured campaign

| # | what it proves | command |
|---|---|---|
| 1 | one video end to end, both arms, images build | `bash run/smoke_run.sh` (AMI, N=1) |
| 2 | **.mp4 routing + long videos + vintage codecs** | `nohup bash run/archive10.sh > ~/logs/archive10.log 2>&1 < /dev/null &` |
| 3 | the films path itself, 8 measured + 2 warm | `SUBSET=10 nohup bash run/films_v2.sh > ~/logs/films10.log 2>&1 < /dev/null &` |

Smoke 2 is the one that matters for this corpus: AMI only ever proved `.avi`, while the
films are `.mp4` with 1960s-era codecs inside h.264 derivatives and ~2.6× AMI's chunk
mass per document. Results land in
`s3://rocketride-benchmark-data/leela/videobench/archive10-<stamp>/`.

---

## 4. Running the benchmark

One runner handles every subset; `SUBSET` picks the pin, `N` defaults to `SUBSET - 2`
(the last two are held out as disjoint warm fixtures).

```bash
cd aws_video

SUBSET=10  nohup bash run/films_v2.sh > ~/logs/films10.log  2>&1 < /dev/null &   # ~30 min
SUBSET=50  nohup bash run/films_v2.sh > ~/logs/films50.log  2>&1 < /dev/null &   # ~2.5 h
SUBSET=500 nohup bash run/films_v2.sh > ~/logs/films500.log 2>&1 < /dev/null &   # ~24 h
```

| variable | default | meaning |
|---|---|---|
| `SUBSET` | 10 | which frozen pin: 10 / 50 / 100 / 500 |
| `N` | `SUBSET-2` | measured films; the remainder are warm fixtures |
| `ARMS` | `rr lg` | which arms run — `rr`, `lg`, or both |
| `PAIR_RR` / `PAIR_LG` | — | rerun one arm and report it against an existing directory for the other |
| `RR_MODE` / `LG_MODE` | `blast` / `c32` | RocketRide one batch; LangGraph 32 concurrent requests |
| `BENCH_TIMEOUT_S` | 86400 | per-run ceiling |
| `RR_PIPE_TTL_S` | timeout + 7200 | **must exceed the timeout** — `ttl` is an idle timer and a short one kills tasks mid-batch |
| `CORPUS_DIR` | `~/bench_corpus_films_v2_<SUBSET>` | local staging directory |

What the runner does before either arm touches a file: verifies the manifest seal
(sha256 vs sidecar vs pin header), checks corpus name, document count and per-document
fields, **hashes every pulled film against the manifest**, and records licence
classification. Then it builds images, captures provenance (image digests, LangGraph
`pip freeze`, model hashes, host and git state), runs the arms one at a time, reports,
and syncs to S3. Any nonzero component makes the run exit nonzero with a single
`FINAL STATUS` line.

---

## 5. Results

Each run writes to `results/films<SUBSET>-<stamp>/` locally and mirrors to:

```
s3://rocketride-benchmark-data/leela/videobench/films<SUBSET>-<stamp>/
```

Existing campaigns:

| run | S3 prefix |
|---|---|
| 10-film smoke (`.mp4` shakedown) | `.../videobench/archive10-20260822T190711Z/` |
| films10 | `.../videobench/films10-20260823T170514Z/`, `...-20260824T030826Z/`, `...-20260825T060011Z/` |
| films50 | `.../videobench/films50-20260823T175555Z/` |
| films500 | `.../videobench/films500-20260824T034901Z/`, `...-20260824T073256Z/`, `...-20260825T061529Z/` |

Read any of them back — gates first, numbers second:

```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/videobench/<run>/ ./run --profile leela
python3 bench/report.py ./run/rr                       # one arm
python3 bench/report.py --arms ./run/rr ./run/lg       # cross-arm gates
```

`report.py` exits nonzero on any hard gate failure. A single repetition is labelled
**SIZING** evidence and cannot pass determinism.

---

## 6. Two things specific to this corpus

**Frame parity is approximate, unlike AMI.** Old prints have variable frame timing, so
RocketRide's reader over-samples by up to ~6% against ffmpeg. Films are "equal work
within ~1.5%", never "identical" — the strict frame-exact claims belong to AMI.

**Long inputs at high concurrency are a memory test.** The first 498-film LangGraph
attempt died at 97 films: the arm decoded every frame of a film into memory before
detection (~2.3 GB per 90-minute 1080p print, 32 in flight) and the host killed it.
The arm now streams frames from disk. If you change the frame path, re-run films500
before trusting it.

---

## 7. Related

- [`ARCHIVE_FILMS.md`](ARCHIVE_FILMS.md) — how the corpus was selected, staged, licensed and sealed
- [`README.md`](README.md) — the harness itself, knobs and gates
- [`METRICS.md`](METRICS.md) — what each gate and metric means
- [`RESULTS.md`](RESULTS.md) — the films campaign results
