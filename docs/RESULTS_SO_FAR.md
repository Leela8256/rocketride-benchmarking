# Results so far

A ledger of the runs that matter, so you can re-derive numbers before
producing new ones. Every row is reproducible from the raw records at the
S3 path given. Evidence grade is stated per row; most of this is sizing
evidence, not the publishable three-repetition comparison, which has not
been run yet.

Box for every row: AWS c7i.8xlarge (32 vCPU, 61 GB), $1.428/h. Engine unless
stated: RocketRide 3.3.1 with the two documented patches, SDK 1.3.0.

## PDF track

Corpus: GovDocs1 PDFs sorted by basename (`corpus/fetch_govdocs.sh`). The
10k set is documents 201 to 10200 plus 24 disjoint warm-up documents
(manifest sha256 `ec2797f0...`). 87 of the 10,000 are text-free on both arms.

| run | date | shape | RocketRide | LangGraph (Tika sidecar) | grade | S3 (`leela/bench/`) |
|---|---|---|---|---|---|---|
| 10k c128, 32 cores | 2026-08-18 | both arms, c128, RR threads=32, 1 rep | 49.85 chunks/s, 18.1 eff cores, p50 28.4 s, max 2,050 s | 120.97 chunks/s, 29.1 eff cores, p50 19.3 s, max 531 s | RUN PASS (determinism by concurrency invariance c128 vs seq) | `20260818T052912Z_c128/` |
| seq 200 | 2026-08-18 | both arms, sequential | p50 1.613 s, 8.70 cpu-s/doc | p50 0.976 s, 2.56 cpu-s/doc | service latency | `20260818T045211Z_seq/` |
| batch10k RR | 2026-09-15 | RR only, b16 / b32 / b64, 24-core arm, 1 rep each | 11.64 / 14.09 / 17.24 chunks/s; 3.79 / 4.63 / 5.64 cores; 6.98 / 7.06 / 7.02 cpu-s/doc (flat); max latency about 1,680 s at every size | none | RUN PASS (cyclic cross-cell determinism 9,913/9,913) | `batch10k-2026-09-15/batch10k-b<B>-<stamp>/` |
| batch10k LG | 2026-09-21 | LG only, same cells | none | 25.53 / 32.74 / 41.79 chunks/s; 2.85 / 4.05 / 5.63 cores; 2.40 / 2.66 / 2.89 cpu-s/doc; max about 220 s | RUN PASS (same basis) | `batch10k-lg-2026-09-21/` |
| PR #2197 A/B | 2026-09-08 | RR only, 1k docs, blast, stock vs length-sorted batching | +0.61 % chunks/s, -1.67 % cpu-s/chunk | none | SIZING, 1 rep | `pr2197pdf-{stock,patched}-20260908T223814Z/` |
| profiler campaign | 2026-10-21 | official 3.5.0.584, 1k docs, one profiled run per arm, isolated cpusets | diagnostic captures (yappi, apaevt_flow) | diagnostic captures | DIAGNOSTIC, not performance | see `aws_pdf/README.md` profiler section |

Headlines that have held across every PDF run: RocketRide spends 1.5x
(c128, threads=32) to 2.9x (b16, default pool) LangGraph's CPU per document;
RocketRide's effective cores plateau at 15 to 18 of 32 at c128 with
threads=32 and sit at 3.8 to 5.6 in the batch cells; and the RocketRide
latency tail is a handful of small PDFs that its parse path takes minutes
on, at every batch size.

## Video track

Corpus `ami_full`: all 170 usable AMI meetings (168 measured + 2 warm,
98.19 h by audio duration, 96.06 h by video stream), staged at
`s3://rocketride-benchmark-data/leela/corpus/ami_full/`. Pipe: detect only
(frames every 15 s, RF-DETR 0.3, split 4000/0, MiniLM).

| run | date | posture | RocketRide | LangGraph | grade | S3 (`leela/videobench/`) |
|---|---|---|---|---|---|---|
| Run C, full corpus | 2026-08-22 | RR default (1 task, blast) vs LG default (1 process, c32), unpinned | 37.43x realtime, 5.98 cores, 9.91 cpu-s/footage-min, $38.15 per 1k footage-h, 2.62 h | 155.65x, 26.84 cores, 10.66 cpu-s/footage-min, $9.17, 37.9 min | SIZING, 1 rep; all gates PASS; frame parity 168/168 | `native170-20260822T070136Z/` |
| RR matched 8x4 | 2026-08-26 | 8 tasks, BLAS 4, sharded blast, admission unbounded | 166.1x, 11.07 frames/s, 23.87 cores, 8.62 cpu-s/footage-min, $8.60, peak 57.4 GB | none | SIZING; task_census 8/8 PASS | `matched8x4-ami-rep1-20260826T064153Z/rr` |
| RR matched 6x4, threads=32 | 2026-08-26 | 6 tasks, BLAS 4, `use(threads=32)` per connection | 177.2x, 11.81 frames/s, 21.39 cores, 7.24 cpu-s/footage-min, $8.06 | none | SIZING; task_census 6/6 PASS | `matched6x4-ami-rep1-20260826T155110Z/rr` |
| LG one-port 8 workers | 2026-08-26 | `uvicorn --workers 8` on one port, torch 4/1, c32 | none | 100.7x, 6.71 frames/s, 11.26 cores, 6.71 cpu-s/footage-min, $14.18; worker skew 49/43/29/15/14/11/4/3 | SIZING; paired with RR matched 8x4 | `matched8x4-ami-rep1-20260826T075823Z/lg` |
| RR thread sweep | 2026-08-24 | 1 task, threads unset / 8 / 32 / 64, 16 videos | 5.93 to 5.97 cores at every setting (flat) | none | probe | `rrsweep-*` |
| films500 | 2026-08-24/25 | 498 Internet Archive films, 674.75 h; RR default blast vs LG c32 | 35.0x, 6.88 cores, 11.79 cpu-s/footage-min, $40.79, 19.27 h | 154.6x, 25.46 cores, 9.88 cpu-s/footage-min, $9.24, 4.36 h | SIZING; LG arm rerun after the streaming fix, paired | `films500-20260824T073256Z/rr`, `films500-20260825T061529Z/` |

Headlines: per-unit CPU is within about 10 % between arms on every video
run; the throughput gap is utilisation. One RocketRide task holds about 6 of
32 cores no matter what `threads` says; eight tasks reach 24. The only
full-corpus pair in matched posture so far is RR 8x4 (166x) against the LG
one-port cell (100.7x), and the one-port cell's kernel skew explains most
of that gap; the LG 8-port matched cell passed its 8-video smoke (RR 160x
vs LG 185x, one video per process) but has not run on the full corpus. See
`aws_video/RESULTS_AMI_POSTURES.md`.

Full write-ups with every metric: `aws_video/RESULTS_AMI_FULL.md`,
`aws_video/RESULTS_AMI_POSTURES.md`, `aws_video/RESULTS_FILMS.md`,
`aws_pdf/RESULTS_PDF.md`.

## What has not been done

- The publishable, enveloped, three-repetition campaign for either track.
- Sequential (`seq`) runs of both video arms for the cross-mode block.
- The LangGraph 8-port matched cell on the full AMI corpus
  (`lg_matched_8x4_c32`); it passed its smoke.
- A fault-injection run (M4 and M5) in native saturation mode.
- Any RocketRide run with the `--modelserver` proxy mode on this box; the
  box is CPU-only and every number here is in-process ("local mode").
