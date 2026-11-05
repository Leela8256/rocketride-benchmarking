# Known traps

Every item here has already cost a run, a night, or a number. Read before
any launch longer than ten minutes.

## Access and the box

1. **SSM sessions need a pseudo-terminal.** Since agent 3.3.4793.0 a session
   without a TTY dies with `Cannot perform start session: EOF` and lingers
   "Connected". Twenty-five zombies hit the per-instance cap and lock the box;
   `TerminateSession` is denied, so only a stop/start or the idle timeout
   clears them. `local/box.sh` wraps every session in `script`. Do not write
   your own session wrapper.
2. **The pseudo-terminal echoes your command.** A marker string inside the
   remote command matches your own grep. Grep on plain `tail` output;
   assemble markers from pieces if you must.
3. **`aws` vanishes from PATH in non-login shells.** The user-dir install
   lands in `~/.local/bin`; nohup does not see it; `matched_run.sh` then
   silently skips the S3 upload. Fix: `sudo ln -sf /home/ssm-user/.local/bin/aws /usr/local/bin/aws`.
   The video runners also fall back to `~/.local/bin/aws` explicitly.
4. **The box auto-stops after about an hour below 20 % CPU**, with no
   warning. Disk survives. Long low-CPU phases (corpus extraction, hashing,
   image builds, report writing) can trip it. Some runners carry a
   keepalive loop: the video matched runner pauses it during measured
   spans, the PDF campaign runners pin it to the last core (inside the
   client's set), and the films, sweep and staging scripts leave it
   running. It is never charged to an arm's cgroup, but it does take a core
   away. Never add a keepalive of your own inside a measured span.
5. **The SSO token expires within hours.** Monitoring from the laptop goes
   blind; the run on the box does not care (instance role). Re-login and
   carry on. The 60 s S3 sync in the video runners means a blind period
   loses nothing.
6. **No port forwarding, no scp, no SendCommand.** Every health check and
   driver runs on the box; code arrives by git; results leave by S3.
7. **Never edit code on the box.** Fix on the laptop, commit, push, pull.
   Editing a running bash script is unsafe, and box-only changes get lost.
8. **The box is shared.** Check `docker ps` before launching; the matched
   runner refuses to start if any container is running. Do not remove
   another person's containers, volumes or corpora.

## Measurement

9. **Only the framework may vary.** The runs before the matched harness
   existed compared LangGraph at 150 documents capped against RocketRide at
   200 uncapped. They were not comparable and were thrown away.
10. **The client must be a separate container on its own cores.** Driver
    CPU inside the engine's cgroup contaminated every early efficiency
    number.
11. **`/proc/stat` is not namespaced.** A sampler reading it reports host
    cores regardless of load. Only cgroup counters.
12. **An arm is service plus sidecar.** Without a shared cpuset the
    LangGraph arm silently gets a second full allocation for Tika. Stop Tika
    before the RocketRide arm.
13. **Memory caps per container are not an arm-level cap.** The PDF track
    measures memory rather than capping it; the video matched postures cap
    both arms at the same 58 GB.
14. **A sampler with a lifetime cap truncates long reps.** `SAMPLE_MAX_S`
    used to be 7200 s and lost coverage on an 18,290 s cell. Default is now a
    day; the report discloses coverage.
15. **Two samplers on one counter interleave.** Merging a supplementary
    sampler stream into the runner's stream produced negative CPU until
    `merge_samplers.py` made the runner stream authoritative.
16. **Timings under emulation are not numbers.** The engine is linux-x64
    only; a Mac runs it under Rosetta or QEMU. Correctness only.
17. **Cross-platform outputs differ by about 1 %** (ffmpeg and torch
    numerics). Gates compare within platform; comparisons are box versus box.

## RocketRide specifics

18. **3.3.0 and 3.3.1 do not boot on Linux** without the onnxruntime pin fix
    (`aws_pdf/findings/rr_linux_boot.md`). The Dockerfile applies it. All
    five requirement files must move together or the failure gets worse.
19. **3.3.1 double-emits some embedding batches.** The
    duplication correction is a build argument; `self_duplication` is a
    permanent gate. The defect was fixed upstream from 3.4.0, so the official
    3.5.0.584 build runs unpatched (`RR_DUP_PATCH=0`) and the gate watches it.
20. **The engine keeps a copy of every uploaded video** in container
    scratch, released only when the container is removed. A 100 GB disk
    filled mid-run once (47 of 60 videos failed with ENOSPC). Teardown
    between arms is load-bearing; corpora larger than disk run in waves.
21. **`RR_HOST=0.0.0.0` must be explicit.** Without it the engine binds an
    interface unreachable from the host, which once looked like a product
    defect ("WebSocket upgrade rejected").
22. **`use(threads=N)` is a per-connection admission cap, not a worker
    pool.** Valid range 1 to 64; `threads=256` is rejected with an error
    frame on every upload. On video, threads did nothing for utilisation; on
    PDF, 32 threads raised effective cores from about 6 to about 17.
23. **One `use()` with `use_existing=True` = one task process.** A task
    holds one detector behind a real lock, so one task uses about 6 of 32
    cores on video. More tasks (distinct `project_id`, no `use_existing`,
    own connections) is how the engine scales; this is the matched posture.
    `ttl=0` means no expiry; a stale `ttl=0` task never dies on its own.
24. **Engine model and node dependencies install at first `use()`**, into
    the model cache volume, and take minutes. Always warm-up, never measured.
25. **The 3-chunk PDF tail is intrinsic.** A handful of small govdocs PDFs
    (039660, 011464, 008871) take 400 to 1,700 s in the engine's parse path
    at every batch size, while LangGraph with the same Tika 3.2.3 parses them
    in seconds. Do not read that tail as queue position.
26. **000164.pdf and 000357.pdf return zero chunks on both arms.** Only the
    first is allowlisted; `CENSUS_EMPTY_POLICY=report` names text-free
    documents without failing the run. The 10k corpus has 87 of them.

## LangGraph specifics

27. **`/meta` reports settings that are inert.** `executor_workers`,
    `max_inflight_requests`, `request_timeout_seconds` are not enforced. The
    executor width is `min(32, cpu_count + 4)` on host cores.
28. **Async nodes serialise compute through the event loop.** Nodes are
    sync functions on purpose.
29. **The wrong MiniLM passes every structural check.** `multi-qa-MiniLM-L6-cos-v1`,
    never `all-MiniLM-L6-v2`.
30. **Decoding every frame up front OOM-kills the service on long films.**
    Frames stream from disk one at a time; uploads stream through
    `requests-toolbelt`.
31. **`uvicorn --workers 8` on one port is kernel-balanced and skews.**
    Observed 49/43/29/15/14/11/4/3 videos per worker. The matched posture
    uses one port per process so the client can address, warm and verify
    each worker.
32. **Requirement files float.** `pip freeze` is captured into every run;
    pin before a measured campaign.

## Corpora

33. **AMI camera files have no audio track.** Each benchmark document is a
    stream-copy mux of `Closeup1.avi` and `Mix-Headset.wav`. Delete the raw
    downloads after muxing; keeping them caused the ENOSPC failure.
34. **About 10 % of AMI meetings have video and audio streams of different
    length.** The manifest carries both the audio duration and the ffprobe
    video-stream duration; the drivers use the probed video duration as the
    footage denominator and for `frame_law` when it is present (Run C
    predates the field and used audio durations, 98.19 h versus 96.06 h
    probed). Say which one a number uses.
35. **The films pin's second column is the upstream filename**, which differs
    from the staged filename for 213 of 500 films. Use column 1 (the archive
    id) plus `.mp4`.
36. **Detection-dense rooms exceed 4000 characters in one frame line.** The
    splitter cuts mid-line; chunks are text windows, not valid JSON, on both
    arms. Faithful replication includes the quirk; the chunk-per-frame bound
    in `frame_law` needs about 3x, not 1.5x, for the densest Idiap rooms.
37. **The AMI mirror at Edinburgh is slow and sometimes unreachable.** The S3
    copy of `ami_full` is the corpus; stage from the mirror only if S3 is
    gone.
38. **Whisper is not deterministic across reps** (temperature fallback flips
    about one segment per 20 minutes). The dual-lane pipe is not the
    benchmark contract for that reason.

## Harness bookkeeping

39. **A backgrounded loop that keeps a function's stdout open hangs
    `CW=$(...)` forever.** Redirect its output. (The census watcher.)
40. **A `*python*` process filter matched the snapshot's own shell.** The
    engine binary is `./engine`; task processes are `node.py /tmp/task-*`.
41. **`WARM` is overloaded** in the video track: a count for the runners, a
    bind mount for compose. `native170.sh` reads it into `WARM_N` and unsets
    it. Do not export `WARM=2` into a compose command.
42. **pgrep patterns self-match.** A watcher that greps for its own command
    line reports itself as running.
43. **The S3 pipe hash is of canonical components**, not the raw file. The
    visual editor rewrites viewport fields on every save, so the raw hash is
    unstable while the pipeline is not.
44. **`cross_mode_determinism.py` prints "NOT INVARIANT" on any single-arm
    run** because the absent arm has no records. Read the per-arm lines.
45. **`aws_pdf/bench/test_langgraph_profile.py` runs inside the profiler
    image** (it loads `/app/bench_profile_app.py`). On a laptop its
    `setUpClass` errors by design; the six tests in `test_profile_capture.py`
    pass with `cd aws_pdf/bench && python3 -m unittest test_profile_capture`.
46. **`REPS=1` ends in `RUN FAIL` by design** on the PDF track: determinism
    is unproven with one rep unless `bench/cross_mode_determinism.py`
    evidence exists. Use `REPS=2` or more for anything that must say PASS.
    The video report marks a single rep `SKIP` and `evidence_grade: SIZING`
    instead of failing.
47. **Compose project names come from the folder.** This repository's
    projects are `aws_pdf` and `aws_video`; the older clone on the box used
    `aws_bench` and `aws_videobench`. Container names are fixed in the
    compose files, so stopped containers from the old projects block
    `compose up` with a name conflict until removed; the model-cache volume
    is per project (`aws_video_rr-model-cache`); `:latest` image tags are
    shared between clones.
48. **Any compose command in `aws_pdf/` needs `CORPUS` set** (the client
    mounts `${CORPUS}:/corpus:ro`; an empty value is an invalid mount spec).
    The runner exports it; by hand use `CORPUS=$HOME`.
49. **The official engine env forces the patch knobs.**
    `aws_pdf/run/official-engine-20261021.env` exports `RR_DUP_PATCH=0`,
    `RR_LENSORT_PATCH=0` and `PARSE_STOCK_URL=` unconditionally, and
    `matched_run.sh` sources it whenever no engine is selected. The older
    campaign scripts (batch10k, nodeprofile, parse_pathology, pr2197) were
    written for 3.3.1 and silently run on the official build unpatched
    unless you source `aws_pdf/run/historical-engine-3.3.1.env` first.
    `environment.txt` always records what actually ran.
50. **`aws_pdf/run/parse_ab_aws.sh` names its bulk cell `rr-traced`**, but
    `nodeprofile.sh` only turns tracing on for a cell named exactly
    `traced`, so that cell runs untraced. Fix the name before reusing it.
51. **The SSM session shell is not bash.** `box.sh run` executes in the
    session's shell; `box.sh launch` runs `nohup bash -c`. Keep commands on
    one line, keep `run` commands POSIX, and source bash-only env files
    (`aws_video/run/official-engine-20261021.env`) only through `launch`.
