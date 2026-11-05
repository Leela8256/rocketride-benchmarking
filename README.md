# RocketRide benchmarking

RocketRide (the pipeline engine, self-hosted) against LangGraph (wrapped in a
FastAPI service we wrote), on one shared AWS box, on two workloads:

| track | folder | pipeline under test |
|---|---|---|
| PDF ingestion | [`aws_pdf/`](aws_pdf/README.md) | parse (Tika) -> chunk -> embed (MiniLM, 384-d) -> return documents |
| Video analysis | [`aws_video/`](aws_video/README.md) | 1 frame / 15 s -> RF-DETR detect -> chunk -> embed (MiniLM, 384-d) -> return documents |

Both arms run the same corpus, in the same order, driven by the same client,
one arm at a time. The PDF track pins each arm and the client to disjoint CPU
sets; the video track runs unpinned on the whole host and samples the client
container separately. **Only the framework varies.**
The knobs that shape a measurement land in the run's environment and
provenance files, and a run with incomplete provenance is not a result.

This repository is self-contained. Clone it, read the files below in order,
and you can reproduce every run we have made without any other context.

## Read in this order

| # | read | you will know |
|---|---|---|
| 1 | [docs/AWS_BOX_ACCESS.md](docs/AWS_BOX_ACCESS.md) | how to log in, start the box, run commands on it, get results back, and what is impossible |
| 2 | [docs/DOCKER_AND_ENVELOPE.md](docs/DOCKER_AND_ENVELOPE.md) | the three containers, the CPU envelope, the pinned engine image and its patches |
| 3 | [docs/LANGGRAPH_FASTAPI.md](docs/LANGGRAPH_FASTAPI.md) | how LangGraph is wrapped in FastAPI for each workload, and why it mirrors the RocketRide pipe stage for stage |
| 4 | [aws_pdf/README.md](aws_pdf/README.md) | the PDF benchmark: corpus, runner, knobs, outputs, gates |
| 5 | [aws_video/README.md](aws_video/README.md) | the video benchmark: corpora, postures, runners, outputs, gates |
| 6 | [docs/METRICS.md](docs/METRICS.md) | every metric, where it is computed, and the rules for quoting it |
| 7 | [docs/KNOWN_TRAPS.md](docs/KNOWN_TRAPS.md) | every trap that has already cost a run. Read before any launch longer than ten minutes |
| 8 | [docs/RESULTS_SO_FAR.md](docs/RESULTS_SO_FAR.md) | what has been measured, with S3 paths, so you can re-derive numbers before producing new ones |

## Repository map

```
rocketride-benchmarking/
  README.md                 this file
  docs/                     the guides listed above
  local/
    box.sh                  the only laptop command that touches the box: login, start, stop, run, launch, tail
    local.env.example       copy to local.env, fill in your SSO profile (git-ignored)
  aws_pdf/                  PDF track (was aws_bench in the original repo)
    README.md               start here for PDF
    docker-compose.yml      both arms + Tika sidecar + the bench client, one equal envelope
    arms/langgraph/         the FastAPI + LangGraph service for PDF
    arms/rocketride/        the pinned, checksummed RocketRide engine image
    pipe/benchmark_pdf.pipe the shared pipeline contract (RocketRide executes it, LangGraph mirrors it)
    bench/                  drivers for both arms, in-container sampler, report
    metrics/                M0 to M7, the only place a metric is computed
    corpus/                 govdocs fetcher (runs on the box)
    run/                    matched_run.sh and the campaign runners
    METRICS_SUITE.md        the metrics contract for PDF
  aws_video/                video track (was aws_videobench in the original repo)
    README.md               start here for video
    docker-compose.yml      both arms + client, default and matched postures
    arms/langgraph/         the FastAPI + LangGraph service for video
    arms/rocketride/        the 3.3.1 engine image for video (same two patches as the PDF 3.3.1 build)
    pipe/                   benchmark_video_detect.pipe (the contract) and the dual-lane variant
    bench/                  drivers (default and matched postures), gates, metrics, report
    corpus/                 AMI fetcher, corpus pins, film corpus tooling
    run/                    runners: smoke, native, matched, sweeps, waves, staging
    DATA_FLOW_PLAN.md, METRICS.md, MATCHED_POSTURE.md, RESULTS_*.md   reference material
```

## The rules

These are not style. Each one exists because breaking it produced a number
that had to be thrown away.

1. **Only the framework may vary between arms.** Same corpus, same N, same
   order, same envelope (PDF: one CPU set per arm; video: the whole host,
   with equal memory limits in the matched postures), same deadline, same
   warm-up policy, same client, same number of repetitions. `aws_pdf/run/matched_run.sh` enforces this
   for PDF; the video runners do the same for video.
2. **One arm at a time.** Never run both arms, or two engine instances, in a
   measured span. Stop the Tika sidecar before the RocketRide arm starts.
3. **The client is a third container.** In the PDF track it is pinned to its
   own cores (`CLIENT_CPUSET`); in the video track it is unpinned but sampled
   separately. Client CPU must never land in an arm's cgroup, and both arms
   must receive their bytes over a network hop.
4. **Gates first, numbers second.** A run that fails a correctness gate keeps
   its data and quotes nothing. `INSUFFICIENT_REPS` or `UNSTABLE` means no
   point value.
5. **Single-repetition runs are sizing evidence**, never benchmark results.
   Three repetitions are the minimum for a quotable comparison.
6. **Timings exist only on the box.** Anything produced on a laptop runs
   under emulation (the engine ships linux-x64 only). Correctness there,
   never numbers.
7. **Label latency by mode.** Closed-loop gives service latency. Blast and
   batch modes give batch-position latency, which includes queue wait. The
   two are never compared.
8. **Provenance or it did not happen.** Image digests, engine binary hash,
   corpus manifest hash, pipe hash, the knobs that shape the measurement.
   `provenance.json` and `environment.txt` are written by the run, not by
   hand.
9. **Describe the engine honestly.** The historical 3.3.1 image carries two
   documented patches and is "RocketRide 3.3.1 with the documented
   embedding-transformer duplication correction", never "stock 3.3.1". The
   PDF track defaults to the unmodified official 3.5.0.584 prerelease build;
   the video track still builds the patched 3.3.1 by default (see
   `aws_video/README.md` section 5.6 for running video on 3.5.0.584).
10. **Always stop the box when you are done.** It costs about $1.43 per hour
    while running. The idle auto-stop exists but is not prompt.

## Quick path: laptop to first result

Prerequisites: the AWS CLI, the Session Manager plugin and an SSO profile
on the laptop (docs/AWS_BOX_ACCESS.md section 3), and the box check in
section 5 of that document (stopped containers left by an older clone block
the first `compose up`).

```bash
# laptop, once
cp local/local.env.example local/local.env       # fill in BOX_PROFILE
bash local/box.sh login                           # browser SSO, you approve
bash local/box.sh start                           # boots the box, waits for SSM
bash local/box.sh run 'test -d ~/rocketride-benchmarking || git clone https://github.com/Leela8256/rocketride-benchmarking.git ~/rocketride-benchmarking'
bash local/box.sh run 'cd ~/rocketride-benchmarking/aws_pdf && bash run/install_awscli.sh && sudo ln -sf /home/ssm-user/.local/bin/aws /usr/local/bin/aws'
bash local/box.sh run 'cd ~/rocketride-benchmarking/aws_pdf && bash run/preflight.sh'

# PDF smoke: 10 documents, both arms, two repetitions (so determinism can pass), closed loop of 4; about 25 minutes incl. image builds
bash local/box.sh launch pdfsmoke 'cd ~/rocketride-benchmarking/aws_pdf && N=10 REPS=2 WARM=2 MODE=c4 CENSUS_EMPTY_POLICY=report bash run/matched_run.sh'
bash local/box.sh tail pdfsmoke 80
# expected: "RUN PASS"; every gate PASS for both arms (comparison_eligible may be false: a 10-document closed-loop run is not a comparison)

# video smoke: 4 AMI meetings from the staged S3 corpus, both arms; about 25 minutes plus the 24 GB corpus pull the first time
bash local/box.sh launch vidsmoke 'cd ~/rocketride-benchmarking/aws_video && RUN_NAME=smoke-n4-$(date -u +%Y%m%dT%H%M%SZ) N=4 WARM=2 LG_MODE=c4 bash run/native170.sh'
bash local/box.sh tail vidsmoke 80
# expected: "FINAL STATUS ... rc_rr=0 rc_lg=0 rc_report=0"; determinism SKIP (single rep, evidence_grade SIZING), every other gate PASS

bash local/box.sh stop                            # ALWAYS
```

Keep every `box.sh run` and `box.sh launch` command on one line; the
details are in docs/AWS_BOX_ACCESS.md section 4.

Video runs sync their results directory to S3 every 60 s while running;
PDF runs upload once, after the report. From any machine with the SSO
profile:

```bash
aws s3 ls s3://rocketride-benchmark-data/leela/bench/ --profile <your-profile>
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/bench/<run>/ ./run --profile <your-profile>
python3 aws_pdf/bench/report.py ./run             # the same numbers, on your laptop
```

## Who does what

- **The laptop** logs in, starts and stops the box, sends commands, pulls
  results from S3, and derives reports. It never reaches a box port.
- **The box** builds images, holds corpora, runs one arm at a time, samples
  CPU and memory from the container cgroups, and uploads results.
- **S3** is the record. Every metric is re-derivable forever from the raw
  per-document records and sampler streams stored there.

## If you are an agent reading this

Follow the documents in the order above. Do not invent commands, flags or
file names: every runner is self-documenting in its header comment, and
every knob is listed in the README of its track. Before launching a run
longer than ten minutes, read `docs/KNOWN_TRAPS.md`. Keep every command you
send to the box on one line. Never run a keep-busy loop during a measured
span, never run two arms at once, and stop the box when the work is done.
