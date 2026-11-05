# Benchmark Metrics Suite — Definitions, Logic, and the Correctness Gate

LangGraph vs RocketRide document-pipeline benchmark.
One document: what we measure, exactly how each number is computed, and the
gate that decides whether any number may be quoted at all.

Implementation lives in `metrics/` (one module per metric family); this file
is the human-readable contract. If code and this file ever disagree, that is
a bug in one of them — fix, don't reinterpret.

---

## Part 1 — The metrics suite

| # | Metric | One-line definition | Implementation |
|---|--------|---------------------|----------------|
| 1 | **Correctness Gate** | Parse, chunk, embed, and census validation must pass before any performance number is valid | `metrics/m0_correctness.py` |
| 2 | **Throughput** | Successful documents per second at each client-concurrency level | `metrics/m1_m2_perf.py` |
| 3 | **Latency** | Per-document end-to-end p50 / p90 / p95 / p99 / max | `metrics/m1_m2_perf.py` |
| 4 | **LLM Tokens / Cost** | N/A for this pipeline — no LLM exists; embeddings are a local CPU model | — (by definition) |
| 5 | **Blast Radius** | Collateral *unrelated* documents damaged by one injected bad document, plus time-to-next-success | `metrics/m4_m5_faults.py` |
| 6 | **Fault Isolation / Recovery** | Error surfacing, service continuity, restart requirement, resource recovery after faults | `metrics/m4_m5_faults.py` |
| 7 | **Lines of Code** | Developer-written code in four layers (pipeline / compute / serving / client) | `metrics/m6_loc.py` |
| 8 | **Resource Efficiency** | CPU, RSS, threads, plus docs-per-CPU-second and CPU-seconds-per-document | `metrics/m7_resources.py` |

**Where the raw data comes from.** Every per-document number originates in
the benchmark driver, not the framework: the driver records a monotonic
timestamp when it submits a document and another when the *verified*
response is fully received, and writes one JSON line per document
(timestamps, outcome, chunk hashes, vector checks). Samplers record CPU/RSS/
threads on a fixed interval alongside. All metrics are **derived afterward
from these raw records** — nothing is accumulated in-flight, so every number
can be recomputed forever from the stored files.

---

## Part 2 — Metrics logic (formulas and rules)

### Throughput

    throughput = successful_completions_in_window / window_span_seconds

- **Closed-loop runs** (fixed N in flight): the window is the whole measured
  run at that level — this is genuine sustained rate.
- **Warm-start rule**: the first `warm_n` *completions* (20 in local gate
  runs; 25 per team directive for AWS runs) are excluded; the span runs from
  the warm boundary to the last completion. Pipes are warm before anything
  counts.
- **Failed documents never count in the numerator.** A run that "finishes
  fast" by failing documents scores lower, not higher.

### Latency

    latency(doc) = completion_timestamp − submit_timestamp   (one monotonic clock)

Percentiles (p50/p90/p95/p99/max) over *successful* documents in the window.

**Mandatory labeling rule** — the same formula means different things under
different load models, so every latency table carries one of two labels:

- **service latency** (closed-loop): the time the service took for a doc.
- **batch-position latency** (open-loop blast): includes queue wait, since
  all docs were submitted at t=0. Mostly measures queue depth, not speed.

The two must never be mixed or compared. LangGraph's per-stage
`Server-Timing` breakdown is diagnostic only (RocketRide exposes no
equivalent, so it can never be a headline).

### LLM Tokens / Cost

Zero, by construction — the pipeline is extract → chunk → embed with a local
model; there is no LLM call. Reporting any nonzero number would be
fabrication. Definitions are reserved for a future LLM-stage pipeline
(prompt/completion tokens per doc, $ per 1k docs).

### Blast Radius (requires a fault-injection run)

Known-bad documents are planted at known positions. For each injected fault:

    blast_radius        = count(UNRELATED docs that failed/stalled/timed out
                                within the attribution window after the fault)
    time_to_next_success = t(first successful unrelated doc after the fault)
                           − t(fault outcome)

The injected doc's own failure is expected and never counted. A healthy
system scores **zero collateral** with recovery under a second.

### Fault Isolation / Recovery (same run)

Per fault, four answers:

- **error_surfaced_by_server** — did the *service* communicate failure (an
  HTTP error status or an error frame)? A success-shaped empty response
  counts as **silent**, even if the client's own checks caught it; that
  distinction is reported separately (`failure_only_inferred_by_client`).
- **service_continued** — did all unrelated documents complete without
  intervention?
- **restart_required** — recorded by the run orchestrator (not derivable
  from records).
- **resource_recovery** — RSS/threads back within tolerance of the
  *pre-fault* baseline (never run-start, which conflates warmup growth).

### Lines of Code

Non-blank, non-comment lines, counted in four layers per arm and never
summed into one headline number:

1. pipeline definition (RocketRide: the `.pipe` JSON; LangGraph:
   graph/nodes/state/adapter)
2. compute/transforms (LangGraph: `workload/`; RocketRide: none — that code
   is engine-internal product code)
3. serving/integration (Dockerfiles, entrypoints, LangGraph `service/`)
4. client/harness (each arm's driver)

The layering is the honesty mechanism: RocketRide's server is *their
product*, LangGraph's server is *our code* — one total would bury that.
Current numbers (commit-pinned): LangGraph 770, RocketRide 259.

### Resource Efficiency

From sampler streams, over the **same window as throughput**:

    effective_cores       = Δcpu_seconds / window_span
    docs_per_cpu_second   = successful_docs / cpu_seconds_in_window
    cpu_seconds_per_doc   = inverse of the above

Plus RSS peak / median / start→end growth (growth during a run is a finding,
not noise) and thread counts — observed values reported *next to* configured
ones (RocketRide threadCount 64; LangGraph executor `min(32, cores+4)`),
because the gap between configured and observed is itself a result.

---

## Part 3 — The Correctness Gate logic

**Purpose.** Performance numbers are meaningless if work was lost,
corrupted, or unstable. The gate is therefore not a score — it is a
precondition. A run that fails any gate keeps its data forever but its
performance numbers are marked INVALID and never quoted.

**Doctrine: fail closed.** A missing field, an unproven state, or a pending
sub-check is a violation — never a pass. Absence of evidence is absence of
correctness. Aggregation requires every check to be exactly `PASS = true`;
`None`, "pending", or any truthy placeholder cannot turn the light green.

The gate is four checks, each catching a distinct failure class:

### Check 1 — Census (catches LOST work)

- Exactly N records for N offered documents — zero silent documents. Every
  doc has either a full completion or an explicit failure with a reason.
- No duplicate document IDs.
- When the corpus manifest is supplied, silent drops are **named**, not just
  counted.
- Failures are bucketed by driver reason or exception type (never by
  truncated message text), with full example messages preserved per bucket.
- **The one escape hatch**: `expected_empty` — a named allowlist of known
  no-text documents (currently only `000164.pdf`, which contains ~12
  characters of extractable text). Such a doc may legitimately return zero
  content: LangGraph reports it as a zero-chunk success, RocketRide as an
  explicit `no_documents`. Any *other* document producing nothing is a
  defect. Nothing else is excused.

### Check 2 — Structure (catches CORRUPTED work)

Every completed document must satisfy, with fields *present and true* —
absence fails:

- at least one chunk (unless allowlisted-empty — and identity must still
  verify even then)
- every vector exactly **384 dimensions** (the pinned model's output)
- every value finite — no NaN/Infinity
- every vector's L2 norm within **0.001 of 1.0** (normalization pinned)
- chunk-hash count equals chunk count
- **identity proven**: RocketRide responses must reference the exact file
  submitted; LangGraph responses must echo the request ID *and* the
  `X-Output-SHA256` header must equal the SHA-256 of the received bytes
  (transport integrity)

Violations are reported per document with the failing fields named.

### Check 3 — Determinism (catches UNSTABLE work)

Run the same documents twice — different runs, or different submission modes
(blast vs sequential). Per document, per arm:

- the **ordered list of chunk hashes must be byte-identical** across runs
- a document whose *outcome flips* (ok in one run, failed in the other)
  fails
- hashes must actually exist — `None` on both sides is unproven, not equal

This one check replaces heavyweight external-reference machinery for
day-to-day validation: each arm must agree with itself, byte for byte,
regardless of concurrency. *Validated for real on 2026-08-13: blast vs
sequential, RocketRide 49/49 and LangGraph 50/50 byte-identical.*

### Check 4 — Ground truth (catches WRONG work, where a reference exists)

- **LangGraph**: byte-exact chunk hashes (and offsets) against the offline
  reference — the same pypdf+splitter code run *outside* the server.
- **RocketRide**: byte-exact against its own sequential capture (Tika is
  engine-internal, so the reference is captured once, then enforced).
- **Tika mode**: with matched extractor versions and config, **cross-arm
  byte equality** becomes checkable — measured 10/10 byte-identical on first
  test.
- Zero reference coverage is a *vacuous* result, reported as such — never a
  pass.

### Plus: the parity fixture (catches MODEL DRIFT)

A fixed text is embedded through both arms before and after each run. All
four vectors must match the stored reference within tolerance (measured:
max difference 1.3e-07). If this fails, every embedding-related number in
the run is suspect.

### The gate verdict

    PASS  =  census.PASS  AND  structure.PASS  AND  determinism.PASS
             AND  ground_truth.PASS (where applicable)  AND  parity.PASS

INVALID runs are retained for diagnosis, never deleted, never quoted.

---

## Validation status of this suite

| Component | How it was tested |
|---|---|
| Census, Structure | Against real runs (gate-50, both arms) + self-check reproduces the original report exactly |
| Determinism | Real: blast vs sequential, both arms, byte-identical |
| Throughput, Latency | Self-check reproduces gate-50 numbers exactly |
| Blast Radius, Fault Isolation | Real fault-injection run (corrupt + encrypted + zero-byte at c4): zero collateral both arms; surfacing asymmetry captured (LangGraph explicit 500/500/400 vs RocketRide success-shaped empties) |
| Lines of Code | Produced real numbers, layer-split verified by hand |
| Resource Efficiency | Sampler parsing validated against gate-50; efficiency arithmetic synthetic-tested |
| Fail-closed doctrine | Synthetic suite: mutated hashes caught, silent failures flagged, unproven states fail |

Test entry points: `python3 -m metrics.selfcheck` (real-data fidelity) and
`python3 -m metrics.test_synthetic` (formula behavior).
