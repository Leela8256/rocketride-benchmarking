# Finding: RocketRide's CPU utilization on video — what we measured, what we tried, what's left to try

Plain-language account of why RocketRide uses only ~5–6 of the box's 32
processor cores on video work, whether anything we control can change
that, and what the honest options are.

## First, a correction to the "2 cores, then improved to 6" impression

Nothing we changed improved utilization from 2 to 6. What happened is
**ramp-up inside a single run**: the engine starts a big batch slowly
(~2–3 cores busy in the first minutes) and works up to ~6 as its
internal queue fills. Every run shows the same curve:

| run | early window | late window | whole-run average |
|---|---|---|---|
| dual-lane 60-video batch (Aug 19) | 2.5 | ~8 peak | ~3.5–5 |
| detect-only 60-video batch (Aug 20) | 4.9 | 6.5 | **5.85** |
| same, engine asked for 32 threads | 4.6 | 6.7 | **5.59** |
| head-to-head, 6-at-a-time (Aug 21) | ~5.0 live | — | in progress |

Different days, different pipes, different sending styles, one constant:
**a ceiling of roughly 6 busy cores out of 32.** For contrast, our
LangGraph service on the *same box, same videos, same 6-at-a-time* was
measured at ~15+ busy cores minutes into its run.

## What we already tried (and what happened)

1. **Asking the engine for more threads** (`threads=32` at pipeline
   start — the knob that quadrupled RocketRide's throughput on the PDF
   benchmark): **no effect on video.** 5.59 cores vs 5.85 default;
   output byte-identical; the request was verified delivered.
2. **Different sending styles** — everything at once (batch), 6 at a
   time, single: the ceiling is the same. It's not the client's doing.
3. **More client connections** (connection pool sizing): no effect —
   the limit is inside the engine, not in how fast we feed it.
4. **Different pipelines** (with/without audio transcription): same
   ceiling, so it's not one specific component's quirk.
5. **Thread accounting** (new metric, Aug 21): under load the engine
   spawns **~1,000 operating-system threads** while keeping ~5 cores
   busy. So it's not short of workers — the workers are waiting on
   something shared.

## What that evidence points to (in plain terms)

The engine's video components appear to funnel their heavy work — frame
decoding and object detection — through **one shared model instance
behind a lock**. A thousand threads can queue at that lock; only one
passes at a time; ~6 cores' worth of work is all that ever runs in
parallel. We can see supporting hints in the node source (explicit
device locks around models), though the engine core is closed, so this
is a strongly-evidenced diagnosis, not a certainty.

## Can we tweak RocketRide to do better? The honest option list

**Knobs that exist and are already proven not to help:**
- `threads` at pipeline start — no effect (above).
- Client-side concurrency/pooling — no effect.
- There is **no configuration field** for per-component worker counts
  anywhere in the engine's published component schemas — we checked
  every schema file. The knob we'd want does not exist.

**Untested ideas that might help, cheap to try (~30 min box time each):**
1. **Two pipeline instances inside one engine.** The engine can host
   multiple running pipelines. If the lock is per-pipeline rather than
   global, two instances of the same pipe, each fed half the videos,
   could double utilization inside one container. Unknown until tried —
   this is the most interesting remaining experiment.
2. **Un-pinning the engine's internal math libraries.** We deliberately
   left thread environment variables untouched in sizing runs; a probe
   with explicit generous settings (e.g. letting the detector's math
   library use more threads per inference) might deepen each single
   inference. This attacks "make each frame faster" rather than "do more
   frames at once" — modest ceiling, but real.
3. **Smaller frame interval as a throughput lever** — not a utilization
   fix (it adds work rather than parallelism), listed only because it
   changes the work mix that hits the lock.

**Options that work but change what's being measured:**
- **Several engine containers side by side** — proven pattern
  economically (each gets its own ~6 cores), but the decision stands
  that the benchmark measures ONE engine as shipped. If ever used
  operationally, results must say "N engines", never "one engine using
  30 cores".
- **A bigger machine** — pointless: the ceiling is per-engine, not
  per-box. 64 cores would idle 58 of them.

**The real fix is upstream.** The evidence package (threads=32 no-op on
video vs 4× on PDFs; ~1,000 threads for 5 busy cores; the device locks
in the node source) is exactly what RocketRide's engineers would need to
add per-component worker pools. This is our third product finding after
the Linux-boot pin and the duplicate-results bug, and the only path to
a genuine fix.

## Bottom line

- The 2→6 story is one run warming up, not an improvement we made.
- ~6 cores is the engine's own ceiling on video; no shipped knob moves
  it — we tested every one that exists.
- Two cheap probes remain genuinely worth running (two pipelines in one
  engine; math-library threading), and we can schedule either in ~30
  minutes of box time.
- For the benchmark, the correct posture is to **measure and report the
  ceiling**, not engineer around it: the utilization gap versus
  LangGraph on identical hardware *is* one of the study's results.
