# DATA_FLOW_PLAN.md — How the data moves: AMI corpus → S3 → RAM → pipeline

The data-flow architecture for benchmarking **RocketRide vs LangGraph** on
the **full AMI Meeting Corpus** (171 meetings, 100.2 h of footage, 170
usable). One engine per arm, one arm at a time — that is the thing being
measured. Written so a teammate with AWS access and this repo can follow
the whole path of a video from the AMI mirror to a benchmark record
without any other context.

Two run shapes, both already validated at small scale:

- **Shape 1 — one-go run**: the whole set in a single batch. For smokes,
  sizing, and validating changes.
- **Shape 2 — wave run**: the full-corpus benchmark. The corpus lives in
  S3 and is processed in slices ("waves") because of two hard resource
  limits explained in §5.

Read alongside: `METRICS.md` (what we measure), `aws_pdf/README.md`
(the envelope discipline this inherits).

---

## 0. The one rule

**Only the framework may vary between arms.** Same corpus, same order,
same envelope, same rep count, same mode, same deadline, same client
driving both. Every knob lands in `provenance.json`. One arm at a time.

## 1. What the pipeline does to one video (both arms)

A corpus document is one muxed AMI meeting: a ~30-min `.avi` containing
DivX video + PCM audio, ~140 MB. The detect pipeline turns it into
embedded, searchable chunks:

```
video (140 MB) ─► 1 frame every 15 s (~120 frames)
              ─► RF-DETR object detection per frame (threshold 0.3)
              ─► one JSON line per frame  [{"label","score","box","centroid"},…]
              ─► lines joined by newlines, split into 4000-char chunks
              ─► each chunk embedded: multi-qa-MiniLM-L6-cos-v1, 384-dim, normalized
              ─► documents (chunk text + vector) returned to the client
```

RocketRide runs this as `pipe/benchmark_video_detect.pipe` inside its
engine; LangGraph runs the identical stages as a FastAPI + StateGraph
service (`arms/langgraph/`). Splitter size and JSON format were recovered
byte-exactly from engine output, so the arms produce comparable work by
construction. Byte-identical output is NOT required; the V0 bands in
METRICS.md define equivalence.

## 2. STEP ONE — getting the corpus into S3 (one-time staging)

S3 is the corpus's permanent home. Everything downstream pulls from S3;
after staging, no run ever touches the AMI mirror again.

The AMI mirror ships video and audio as SEPARATE files (the camera `.avi`
has no audio track), so staging includes a mux:

```
 (a) DOWNLOAD   mirror → box EBS          ~7–8 MB/s (university server — slow, one time)
     for each meeting:  <mtg>.Closeup1.avi  (video, no audio)
                        <mtg>.Mix-Headset.wav (16 kHz mono PCM)

 (b) MUX        on the box                 ffmpeg stream-copy (no re-encode, seconds/video)
     Closeup1.avi + Mix-Headset.wav  →  <mtg>.avi   (one playable file = one benchmark doc)
     + corpus_manifest.json  (per-video duration_s — the metrics need it)
     + SHA256SUMS            (content identity, verified on every reuse)

 (c) UPLOAD     box → S3                   aws s3 cp --recursive (parallel multipart)
     s3://rocketride-benchmark-data/leela/corpus/<set>/   ← canonical corpus
     verify: object count == manifest count

 (d) CLEAN      delete from the box: the raw downloads AND the muxed EBS copy.
     From here S3 is the only copy. (Everything is rebuildable from the
     mirror by fetch_ami.sh if S3 were ever lost.)
```

Numbers for the full corpus: 170 videos ≈ **24 GB** in S3 (~$0.55/month).
Staging wall time is mirror-bound: ~2 h, once. All of (a)–(d) is
`corpus/fetch_ami.sh` + an upload loop; the 30-video set followed exactly
this path and lives at `…/corpus/ami30test/` today.

Prerequisite before full-corpus staging: extend fetch_ami.sh's meeting
list from the scenario series (ES/IS/TS) to also cover EN/IB/IN — that is
what takes it from 135 to all 170 usable meetings. (TS3003d is skipped:
the mirror has no Closeup1 for it.)

## 3. STEP TWO — getting data back for a run: the RAM staging step

When a run needs videos, it does **not** copy them onto the EBS disk.
It pulls them from S3 straight into **RAM**, via `/dev/shm`:

**What `/dev/shm` is:** a directory that looks like a normal folder but
is backed by memory, not disk (a tmpfs). Files written there occupy RAM;
deleting them frees that RAM instantly. The box has 61 GB of memory and
`/dev/shm` is allowed ~30 GB of it.

**Why we stage through RAM instead of disk:**

1. **It keeps the corpus off EBS entirely.** The 100 GB disk then has
   only one consumer during a run — the engine's own scratch (§5) — which
   makes disk budgeting trivial and removes the failure mode that killed
   an early run (disk full mid-batch).
2. **It's fast and free.** S3 → RAM measured at **158 MB/s** (3.6 GB in
   23 s). A full wave stages in under a minute; the same pull from the
   AMI mirror would take half an hour.
3. **Cleanup is guaranteed.** `rm -rf /dev/shm/wave` returns the memory
   immediately — nothing lingers between waves.

```
S3 corpus  ──(aws s3 cp --recursive, ~158 MB/s)──►  /dev/shm/<wave>/   [RAM]
                                                        │
                                        bind-mounted read-only into the
                                        CLIENT container as /corpus
```

The constraint this creates: **one wave must fit in ~30 GB of RAM.**
AMI waves of 60–100 videos are 8–14 GB — comfortable.

## 4. STEP THREE — through the pipeline (what the client and engine do)

The client and the engine are separate containers, deliberately: the
client's CPU must never be charged to the arm being measured, and the
videos must cross a network hop like they would in production.

```
/dev/shm (RAM)                    client container                arm container
┌───────────────┐   reads files   ┌──────────────────┐  sends    ┌─────────────────────┐
│ w01_0001.avi  │ ──────────────► │ bench_video.py   │ ────────► │ RocketRide engine   │
│ w01_0002.avi  │                 │  - ONE batched   │ websocket │  (or LangGraph      │
│ …             │                 │    send_files    │  / HTTP   │   service)          │
│ manifest.json │                 │    (blast) or    │           │                     │
└───────────────┘                 │    per-video     │           │ processes each video│
                                  │    requests      │           │ through §1's stages │
                                  │  - stamps per-   │ ◄──────── │                     │
                                  │    video events  │  documents│ RR ALSO writes its  │
                                  │  - hashes chunks │  (chunks+ │ own copy of every   │
                                  │  - writes records│  vectors) │ upload to EBS       │
                                  └──────────────────┘           │ scratch — see §5    │
                                          │                      └─────────────────────┘
                                          ▼
                          results dir on EBS (small: JSON records, KBs/video)
                                          │
                              aws s3 sync every 60 s, WHILE RUNNING
                                          ▼
                    s3://…/leela/videobench/<run>-<stamp>/   (live, nothing waits
                                                              for the run to finish)
```

What comes back per video: the documents (chunk text + 384-dim vectors).
The client verifies structure, hashes every chunk, records timings and
per-video completion events, and appends to `per_doc.jsonl` +
`progress.jsonl`. Those records — not the videos — are the benchmark's
output, and they stream to S3 continuously, so a crash or a box
auto-stop can never lose more than the last minute of records.

## 5. Why waves: the two resource limits, and the wave cycle

Two measured facts force the full corpus to be processed in slices:

1. **RocketRide keeps a copy of every uploaded video in its container's
   scratch space and never deletes it** — not when the video finishes,
   not when the batch returns, not even on `terminate()`. The space is
   only released when the **container is removed**. Left unchecked, 170
   videos deposit ~24 GB of dead uploads on the 100 GB disk; thousands
   would deposit hundreds of GB. (LangGraph deletes its temp file per
   request — this limit is RR-specific, and it is itself a reported
   metric: storage amplification, METRICS.md V4.)
2. **RAM staging holds ~30 GB** — one wave, not the whole corpus.

The wave cycle resets both, every time:

```
for each wave (W ≈ 60–100 videos, full AMI = 2–3 waves):

   1. STAGE     S3 ──► /dev/shm/wave          (~1 min, RAM)
   2. BOOT      start ONE engine container    (~1 min; model cache volume
                (or the LG service)            is persistent — no downloads)
   3. PROCESS   client drives the wave through the arm (§4)
                RR scratch on EBS grows ~ wave size (8–14 GB)   ← bounded!
   4. COLLECT   records already in S3 (60 s live sync)
   5. TEARDOWN  remove the engine container   → RR scratch released
                delete /dev/shm/wave          → RAM released
   6. MARK      wave recorded in waves_done (synced to S3) → a crash or
                interruption resumes at the first unfinished wave
```

Disk and RAM therefore breathe back to baseline every cycle; the
campaign's peak footprint is ONE wave, no matter how big the corpus is.
Teardown costs ~1–2 min per wave — noise against a wave's processing
time. Implementation: `run/run_waves.sh` (one engine; resumable;
S3-read preflight).

## 6. The matched benchmark (what makes the numbers publishable)

- **One engine, one arm at a time**, under the equal envelope copied from
  `aws_pdf/docker-compose.yml`: shared `BENCH_CPUSET`, intra-op threads
  pinned (`OMP_NUM_THREADS=1`), one `BENCH_TIMEOUT_S`, memory uncapped
  but measured, client pinned to its own cores.
- **We measure the engine as it ships.** RocketRide's video path holds
  ~6 effective cores regardless of configuration (threads=32 proved it);
  that ceiling is a finding to report, not something the harness works
  around.
- **3 reps minimum** per arm per mode (determinism needs ≥2, CV needs 3).
  Modes: `seq` (true service latency), `c8` (bounded concurrency),
  `blast` / native ingestion (each arm's own path — see aws_pdf README
  for why forcing one interface on both misbenchmarks).
- Gates and metrics: METRICS.md V0–V5, fail-closed. Scale expectation:
  full AMI ≈ 100 h footage → one RR rep ≈ 2.5–3 h at the measured
  36–40× realtime; the whole matched campaign fits in about two box-days.

## 7. Where everything lives

| thing | location |
|---|---|
| canonical corpus | `s3://rocketride-benchmark-data/leela/corpus/<set>/` (+manifest) |
| results (live-synced) | `s3://rocketride-benchmark-data/leela/videobench/<run>-<stamp>/` |
| pipe contract | `aws_video/pipe/benchmark_video_detect.pipe` |
| RR engine image | `aws_video/arms/rocketride/Dockerfile` — 3.3.1 SHA-pinned + boot fix + duplication patch; describe as "3.3.1 with the documented duplication correction", never stock |
| LG arm | `aws_video/arms/langgraph/` (service + Dockerfile) |
| drivers | `aws_video/bench/bench_video.py` (RR), `bench/lg_driver.py` (LG — same record schema), `capture_one.py` |
| run scripts | `aws_video/run/*.sh` — each is self-documenting |
| box control (laptop) | `local/box.sh` — start/stop/run/launch/tail |
| model caches | docker volume `rr-model-cache` (shared by both arms' containers) — keep it; it is why nothing re-downloads |

## 8. Known traps (every one has already bitten)

1. **SSM sessions need a pty** (agent ≥3.3.4793.0) — box.sh handles it;
   zombie sessions pile to the 25-per-instance cap and lock the box.
2. **`aws` vanishes from PATH in nohup shells** — scripts resolve
   `~/.local/bin/aws` explicitly.
3. **RR scratch retention** (§5): never plan disk as if uploads free
   themselves; teardown is load-bearing.
4. **Delete the raw AMI cache after muxing** — keeping it caused the
   ENOSPC failure.
5. **SSO tokens expire within hours** (laptop only; the box role never
   expires — detached runs are safe).
6. **The box auto-stops when idle** — always `nohup`-detach; the 60 s
   live sync makes interruption harmless.
7. **`miniLM` ≠ all-MiniLM-L6-v2** — it is multi-qa-MiniLM-L6-cos-v1;
   the wrong model passes every structural check and is silently
   incomparable.
8. **Detection-dense rooms (IB/IS ≈ 16 objects/frame) exceed 4000 chars
   in one frame-line** → the splitter cuts mid-line, chunks aren't valid
   JSON, and chunks > frames — in BOTH arms, faithfully.
9. **Whisper (dual-lane pipe only)** flips ~1 segment per 20 min across
   reps → determinism gate fails there. The detect pipe is unaffected.
10. **Cross-platform outputs differ ±1%** (ffmpeg/torch numerics): gates
    are within-platform; all comparisons are box-vs-box.

## 9. Open work items before the full-corpus campaign

| item | size |
|---|---|
| extend fetch_ami.sh meeting list to EN/IB/IN (135 → 170) | small |
| stage full corpus to S3 (§2), verify, delete EBS copy | one command + ~2 h mirror time |
| envelope wiring for video (§6) — port from aws_pdf | medium |

## 10. Teammate quickstart

```bash
# laptop (needs: aws cli + session-manager-plugin + SSO access, repo clone)
aws sso login --profile leela
bash local/box.sh start
bash local/box.sh run 'cd ~/rocketride-benchmarking && git pull --ff-only origin main'
bash local/box.sh launch <name> 'cd ~/rocketride-benchmarking/aws_video && bash run/<script>.sh'
bash local/box.sh tail <name>          # poll progress
# results stream to S3 continuously; analyze from any machine:
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/videobench/<run>/ ./run --profile leela
bash local/box.sh stop                 # ALWAYS stop when done
```
