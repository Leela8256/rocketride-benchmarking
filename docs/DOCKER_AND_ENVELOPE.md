# Docker and the envelope

How the arms are built, how they are held equal, and how a run orchestrates
them. The compose files are the source of truth; this is the map.

## 1. Three containers, not two

Every measured run has three kinds of container:

| container | role | PDF name | video name |
|---|---|---|---|
| RocketRide arm | the engine, serving the `.pipe` over WebSocket on 5565 | `bench-rocketride` | `videobench-rocketride` (`-m8` in the matched posture) |
| LangGraph arm | our FastAPI service around a compiled StateGraph, HTTP on 8100 (PDF) or 8200 (video) | `bench-langgraph` (+ `bench-tika` sidecar) | `videobench-langgraph` (`-m8`, `-w8`, `-ip` variants) |
| the client | runs BOTH drivers, reaches each arm over the compose network | `bench-client` | `videobench-smoke` |

The client is a separate container for two reasons that each invalidated
earlier runs. First, the RocketRide driver used to run inside the engine's
container, so its SDK reads, WebSocket framing and JSON parsing were charged
to the engine's cgroup while LangGraph's driver ran on the host and was
charged nothing. Second, RocketRide read container-local disk while LangGraph
paid for an HTTP upload inside its measured latency. With the client on its
own cores both arms pay nothing for the client and both receive their bytes
over an equivalent network hop.

An **arm** is the service plus whatever it offloads to. In the PDF track the
LangGraph arm is `langgraph` plus the `tika` sidecar, because parsing happens
in the sidecar; the report charges Tika's CPU to LangGraph. RocketRide's Tika
is embedded in the engine, so it was always charged for parsing.

## 2. The envelope

| dimension | rule |
|---|---|
| CPU | every container in an arm shares one `BENCH_CPUSET`. The client gets `CLIENT_CPUSET`, disjoint. `matched_run.sh` derives both from `ARM_CPUS` (default three quarters of the host: cores 0-23 to the arm, 24-31 to the client). Empty cpuset = whole host, client not isolated, and provenance records `client_isolated=false` |
| SMT | on this box CPUs 0-15 and 16-31 are sibling pairs. The historic `0-23` / `24-31` split shares eight physical cores between arm and client. For a physically isolated 24-vCPU arm use `BENCH_CPUSET=0-11,16-27 CLIENT_CPUSET=12-15,28-31 ARM_CPUS=24` on both arms (a new hardware posture, needs fresh paired measurements) |
| intra-op threads | `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false` on BOTH arms in the PDF track. Document-level concurrency is the variable under test; BLAS threading is a confound. The video matched postures set the six BLAS/OMP variables to the same value on both arms instead |
| memory | deliberately NOT capped in the PDF track. A per-container cap gave the two-container LangGraph arm twice RocketRide's ceiling, and a 10 GB cap would have OOM-killed RocketRide (peak 10.5 GB). Memory granted is equal (unconstrained on both sides); memory used is a reported outcome. The video matched postures use an explicit 58 GB limit on both arms so a blow-up is a contained, visible OOM (exit 137) rather than a host-wide kill |
| deadline | one outer `BENCH_TIMEOUT_S` for both arms (PDF default 3600 s; video 21600 s native, 86400 s matched). Tika's own 120 s subcall timeout inside the LangGraph arm is disclosed, not hidden |
| Tika | stopped before the RocketRide arm starts, so an idle JVM is not sitting on the arm's cores |
| warm-up | disjoint warm-up documents, timed separately, excluded from every metric |

## 3. The RocketRide image

`aws_pdf/arms/rocketride/Dockerfile` (the video copy is identical apart from
probe fixtures) does, in order:

1. `python:3.12-slim-bookworm` plus `libc++1 libc++abi1` (hard runtime
   dependencies of the C++ engine binary).
2. Downloads the engine release tarball named by `ENGINE_URL`, optionally
   checks the archive against `ENGINE_ARCHIVE_SHA256`, extracts it flat, and
   checks the extracted `engine` binary against `ENGINE_SHA256`. A mismatch
   fails the build on purpose: a silent engine swap would invalidate every
   artifact. Empty `ENGINE_SHA256` prints the hash instead of verifying
   (for experimental builds); provenance still records the hash that ran.
3. Optional parse-node overlay (`PARSE_STOCK_URL`): replaces
   `nodes/parse/libparse.so` and `java/lib/rocketride-parse.jar` from a
   second tarball, for the parse-node A/B of PR #2746. Empty = as shipped.
4. The Linux boot fix: rewrites `onnxruntime-gpu==1.20.1` to `1.20.2` in
   every requirements file. 1.20.1 of the GPU package was never published,
   and the engine compiles all requirements at boot, so 3.3.0 and 3.3.1
   cannot start on any Linux host without this. Full story in
   `aws_pdf/findings/rr_linux_boot.md`.
5. `RR_DUP_PATCH` (default 1 in the Dockerfile): adds
   `return self.preventDefault()` after the embedding flush in
   `nodes/embedding_transformer/IInstance.py`. Without it 3.3.1 emits every
   batch of 64 or more chunks twice (51 of 987 PDFs came back `[A,B,C,A,B,C]`).
   This modifies the product under test, so it is a build argument, recorded
   in an image label and in provenance. The permanent gate
   `self_duplication` catches a regression without needing the other arm.
6. `RR_LENSORT_PATCH` (default 0): the PR #2197 length-sorted embedding
   batching, for A/B runs only.
7. Installs the client SDK `rocketride==1.3.0` and the entrypoint, which boots
   `./engine ai/eaas.py --host 0.0.0.0 --port 5565`. `--host` must be
   explicit; without it the engine binds an interface unreachable from the
   host.

No `.pipe` is baked in. The client owns the pipeline definition and sends it
with `use()`; a second copy drifted from the canonical one within a day.

### Which engine is the default

| engine | how | describe as |
|---|---|---|
| **official 3.5.0.584** (current default) | `aws_pdf/run/official-engine-20261021.env`: prerelease tarball, archive and binary hashes pinned, `RR_DUP_PATCH=0`, `RR_LENSORT_PATCH=0`. `matched_run.sh` sources it automatically when neither `ENGINE_URL` nor `ENGINE_VERSION` is set | "the unmodified official 3.5.0.584 prerelease build" |
| historical 3.3.1 | export `ENGINE_VERSION=3.3.1`, the 3.3.1 `ENGINE_URL` and `ENGINE_SHA256` from the Dockerfile defaults, `RR_DUP_PATCH=1` | "RocketRide 3.3.1 with the documented embedding-transformer duplication correction", never "stock 3.3.1" |

For the video track the 3.3.1 image is what `aws_video/arms/rocketride/Dockerfile`
builds (tag `videobench-rocketride:3.3.1`). To run video on the official
engine instead, build the PDF arm's image with the official env and tag it,
then select the override compose file:

```bash
cd ~/rocketride-benchmarking/aws_pdf && . run/official-engine-20261021.env && docker compose build rocketride
docker tag bench-rocketride:latest bench-rocketride:official-87f530f
cd ~/rocketride-benchmarking/aws_video && . run/official-engine-20261021.env   # sets COMPOSE_FILE to include docker-compose.official.yml
docker compose config --images | grep rocketride                               # must print bench-rocketride:official-87f530f
```

`87f530f` is the engine's own build hash (`/version` reports 3.5.0.584 /
87f530f3). After every engine upgrade revalidate the effective parser,
chunker and model settings: the engine's `services.json` decides what the
`miniLM` profile resolves to.

## 4. The LangGraph images

PDF (`aws_pdf/arms/langgraph/Dockerfile`): `python:3.12-slim-bookworm`, thread
pins as `ENV` (they must exist before torch initialises), pinned
`pypdf==6.15.0` (it defines the offline reference), the MiniLM model baked
into the image at build time, then `HF_HUB_OFFLINE=1` so a cache miss fails
loudly instead of downloading different weights mid-run. One uvicorn worker
is the deployment shape. `Dockerfile.profile` derives from this image and
adds pinned yappi for the profiler campaigns.

Video (`aws_video/arms/langgraph/Dockerfile`): adds ffmpeg from the distro;
models (RF-DETR checkpoint, MiniLM) download at first boot into
`/root/.cache`, which is the shared `rr-model-cache` volume, so the price is
paid once per box.

The LangGraph requirement files use version ranges (`>=`). Pin with
`pip freeze` (the runners save it into the run) before any measured
campaign; the rfdetr 1.9 import relocation is the cautionary tale.

## 5. The model cache volume (video)

Both video arms mount the docker volume `rr-model-cache` at `/root/.cache`.
The RocketRide engine installs the video node dependencies (torch tree,
faster-whisper, rfdetr) and downloads weights at the first `use()` of a
video pipe; with the volume that happens once per box, not once per
`compose up`. Keep the volume. First `use()` on a fresh volume takes
minutes and must always be treated as warm-up, never measured.

## 6. How `matched_run.sh` orchestrates a PDF run

1. Gates: x86_64 only; fetch or verify the corpus for `N`, `CORPUS_OFFSET`,
   `WARM` (directory name encodes all three so a stale corpus can never be
   reused); checksum it.
2. Writes `environment.txt` (host, kernel, docker, git SHA, every knob).
3. Builds the arms selected by `SKIP_LG` / `SKIP_RR` and the client image;
   records image IDs.
4. LangGraph arm: `up -d tika` (if `LG_EXTRACTOR=tika`), `up -d langgraph`,
   wait for the warm-up-gated health check, save `/meta` and `pip freeze`,
   then for each rep start the in-container cgroup sampler
   (`bench/cgroup_sampler.py`, 0.5 s, cgroup v2 `cpu.stat`), run
   `bench/lg_driver.py` in the client, stop the sampler, settle 10 s. Then
   `stop langgraph tika`.
5. RocketRide arm: `up -d rocketride`, wait healthy, save the engine binary
   hash and boot log, same per-rep loop with `bench/rr_driver.py`, then stop.
6. `run/write_provenance.py` writes and checks `provenance.json`.
7. `bench/report.py` derives every metric; exit status is the gate verdict.
8. Uploads the run directory to `BENCH_S3/<RUN_TAG or stamp>/`.

The video runners follow the same shape (`run/native170.sh`,
`run/matched8x4_ami.sh`), with the sampler reading the cgroup from the host
every 15 s, a 60 s live S3 sync, and container removal after each arm
because the engine keeps a copy of every uploaded video in its scratch until
the container is removed.

## 7. Compose services in the video track

| service | container | what it is |
|---|---|---|
| `rocketride` | `videobench-rocketride` | default posture: one engine, env untouched, model cache mounted |
| `langgraph` | `videobench-langgraph` | default posture: one uvicorn, shared model singletons, 52 GB limit |
| `smoke` | `videobench-smoke` | the client (profile `client`, used via `compose run`) with every driver knob forwarded |
| `rocketride-matched` | `videobench-rocketride-m8` | matched posture: six BLAS/OMP vars = `RR_BLAS_THREADS`, 58 GB |
| `langgraph-matched` | `videobench-langgraph-m8` | matched posture: `serve8.py`, `LG_WORKERS` single-worker uvicorns on `LG_PORT_BASE`+i, torch threads pinned, one predict at a time per worker |
| `langgraph-workers8` | `videobench-langgraph-w8` | the "obvious" deployment: `uvicorn --workers 8` on one port, kernel-balanced |
| `langgraph-inproc` | `videobench-langgraph-ip` | one uvicorn, one model copy, inference semaphore of `LG_EXPECT_DETECT_CONC` |

`extends` needs compose 2.20 or newer; the box runs 5.x.

## 8. Laptop builds

Both arms are `platform: linux/amd64` because the engine ships linux-x64
only. On an arm64 Mac they build and run under emulation. Use that for
correctness checks of driver or service changes, never for a number.
