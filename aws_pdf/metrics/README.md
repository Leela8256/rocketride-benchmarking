# Metrics — canonical logic for M0–M7

One place for every metric's definition, formula, and implementation.
The spec (`LangGraph_vs_RocketRide_Benchmark_Specification.docx` §2) defines
WHAT each metric is; this folder is the only place that defines HOW it is
computed. Older checkers (`gate50/check_gate50.py`, `pdf200/validate200.py`,
`pdf500/census.py`) predate this folder and are being migrated onto it.

All inputs are the per-doc JSONL records the drivers write (one line per
document: submit/completion nanoseconds, outcome, chunk hashes, vector
checks) plus the sampler JSONL streams. Every metric is **derived after the
run from raw records** — nothing is accumulated in-flight, so any number can
be recomputed forever.

---

## M0 — Correctness gate  → `m0_correctness.py`

**Logic:** performance numbers are meaningless if work was lost, corrupted,
or unstable — so correctness is a GATE, not a score.

**Doctrine — fail closed.** A missing field, an unproven state, or a
pending sub-check is a violation, never a pass. Structure enforces a
per-arm field contract (`REQUIRED_TRUE`): RR records must carry
`identity_ok=True`; LG records additionally `sha_header_ok=True` and
`vectors_finite=True` — absence fails, it does not default to pass. The one
sanctioned escape hatch is `expected_empty`: a named set of known no-text
docs (today only `000164.pdf`) allowed to produce zero content, either as a
zero-chunk completion (LG) or an explicit `no_documents` failure (RR); any
other doc producing nothing is a defect. `gate_verdict()` requires `PASS`
to be exactly `True` — `None`, `"PENDING"`, or a truthy placeholder can
never aggregate to green.

Four checks, each catching a distinct failure class:

1. **Census** (`census()`): loss detection. offered == records, unique doc
   ids, zero silent documents, zero unexpected failures. When the corpus
   manifest (`expected_docs`) is supplied, silent drops are NAMED
   (`missing_docs`), not just counted. Failures bucket by driver reason or
   exception type — never by a truncated message prefix — with distinct
   full messages kept per bucket in `failure_examples`.
2. **Structure** (`structure()`): corruption detection. Every completed doc:
   ≥1 chunk (or allowlisted-empty, identity still verified), every vector
   exactly 384-dim, finite, L2 norm within 1e-3 of 1.0, chunk-hash count ==
   n_chunks, identity verified (RR: filepath echo; LG: request_id echo +
   X-Output-SHA256 == sha256(body)). Violations are reported per doc.
3. **Determinism** (`determinism()`): instability detection. Ordered chunk
   hashes from run A must equal run B per doc, per arm; a doc whose OUTCOME
   flips between runs (ok in one, failed in the other) also fails. The arm
   must agree with itself byte-for-byte across runs/modes.
4. **Ground truth** (`ground_truth_match()`): absolute-correctness tier
   where a reference exists (LG: offline pypdf refs; RR: sequential capture;
   tika mode: cross-arm byte equality is additionally possible).

Plus `parity_fixture()`: the same fixed text embedded through both arms must
produce vectors within allclose tolerance — catches model/config drift.

**PASS rule:** all applicable checks green, else the run's performance
numbers are INVALID (kept, never quoted).

## M1 — Throughput  → `m1_m2_perf.py::throughput()`

**Logic:** successful documents per second over a well-defined window.

    M1 = successful_completions_in_window / window_span_seconds

- Closed-loop runs: window = whole measured run at that concurrency level;
  this is genuine sustained rate at that level.
- Warm-start rule (when enabled): the first `warm_n` COMPLETIONS (20 for
  gate runs, 25 per team directive for AWS runs) are excluded; span runs
  from the warm boundary to last completion.
- Failed documents never count in the numerator. Ever.

## M2 — Latency  → `m1_m2_perf.py::latency()`

**Logic:** per-document end-to-end time as the CLIENT experiences it:

    latency(doc) = completion_ns - submit_ns   (same monotonic clock)

Report p50/p90/p95/p99/max over successful docs in the window.
**Labeling rule (mandatory):** closed-loop runs yield *service latency*;
open-loop blast runs yield *batch-position latency* (includes queue wait —
all docs submitted at t=0). The two must never be mixed or compared.
LangGraph's Server-Timing stage breakdown is diagnostic detail, never the
headline (RocketRide exposes no equivalent).

## M3 — LLM tokens / cost  → no code, by definition

**Logic:** this pipeline contains no LLM; embeddings are a local CPU model.
Tokens = 0, cost = $0. The metric is reserved: if a future pipeline adds an
LLM stage, count prompt/completion tokens per doc from the provider response
and price them; until then any nonzero number here would be fabricated.

## M4 — Blast radius  → `m4_m5_faults.py::blast_radius()`

**Logic:** when one document fails, how much OTHER work did it damage?
Requires a fault-injection run: known-bad docs at known positions
(`fault_manifest`). For each injected fault:

    blast_radius = count(unrelated docs that failed/stalled/timed out
                         in the window attributable to the fault)
    time_to_next_success = t(first successful unrelated doc after fault)
                           - t(fault outcome)

A healthy system scores 0 collateral / near-zero recovery time. The metric
only counts OTHER docs — the injected doc's own failure is expected.

## M5 — Fault isolation / recovery  → `m4_m5_faults.py::fault_isolation()`

**Logic:** four booleans + one delta per fault, answering "did the system
tell us, survive, and clean up?":

- error_surfaced: did the client receive an explicit error (vs silence)?
- service_continued: did subsequent unrelated docs complete without
  intervention?
- restart_required: did recovery need a process/container restart?
- resource_recovered: RSS/threads within tolerance of pre-fault baseline
  after the fault (from sampler stream) — leak detection.

## M6 — Lines of code  → `m6_loc.py`

**Logic:** developer effort proxy, counted honestly in four layers so
"RocketRide's server is their product, LangGraph's server is ours" stays
visible instead of hidden in one number:

1. pipeline definition (RR: the .pipe JSON; LG: graph/nodes/state/adapter)
2. compute/transforms (LG: workload/; RR: none — engine-internal)
3. serving/integration (Dockerfiles, entrypoints, LG service/)
4. client/harness (the drivers each arm needs)

Count = non-blank, non-comment lines (pure-Python counter, no cloc
dependency). Static — computed once per git commit, no run needed.

## M7 — Resource efficiency  → `m7_resources.py`

**Logic:** absolute footprint + work-per-resource, from the 100 ms/500 ms
sampler streams over the SAME window as M1:

- effective_cores = Δcpu_seconds / window_span (container sampler) or
  mean %CPU/100 (native ps sampler)
- rss: peak, median, start→end growth (growth during a run is a finding)
- threads: peak, median — observed, reported next to configured
  (RR threadCount 64 / LG executor width)
- efficiency: docs_per_cpu_second = M1_successes / cpu_seconds_in_window;
  cpu_seconds_per_doc = its inverse

---

### File map

| Spec ID | Module | Key functions |
|---|---|---|
| M0 | `m0_correctness.py` | `census`, `structure`, `determinism`, `ground_truth_match`, `parity_fixture`, `gate_verdict` |
| M1, M2 | `m1_m2_perf.py` | `throughput`, `latency`, `perf_window` |
| M3 | — (README only) | N/A by definition |
| M4, M5 | `m4_m5_faults.py` | `blast_radius`, `fault_isolation` |
| M6 | `m6_loc.py` | `loc_report` (runnable: `python3 -m metrics.m6_loc`) |
| M7 | `m7_resources.py` | `container_resources`, `native_resources`, `efficiency` |
| shared | `records.py` | `load_records`, `Record` helpers |

`selfcheck.py` re-derives yesterday's gate-50 numbers from raw records via
this library and compares them to `gate50/GATE50_REPORT.json` — run it after
any change here.
