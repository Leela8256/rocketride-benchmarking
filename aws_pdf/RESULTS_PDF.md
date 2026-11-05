# PDF Pipeline Benchmark — Environment, Configuration and Results

**RocketRide on the govdocs PDF corpus: 1,000 documents at default posture,
run twice to measure the effect of a change to embedding batch composition.**

| | |
|---|---|
| campaign | `pr2197pdf` · stamp `20260908T223814Z` |
| date | 2026-09-08, 22:38–22:58 UTC (~20 min total) |
| arms | RocketRide only (`SKIP_LG=1`) — LangGraph neither built nor run |
| documents | 1,000 measured + 25 disjoint warm-up (excluded) |
| repetitions | 1 per cell |
| status | **SIZING evidence, not publishable** — single rep, unpinned threads, three gates fail closed by design |
| records | `results/pr2197pdf-stock-20260908T223814Z`, `results/pr2197pdf-patched-20260908T223814Z` |

---

## Verdict

| metric | stock | patched | delta |
|---|---:|---:|---:|
| **docs/s** | 2.0225 | **2.0348** | **+0.61 %** |
| **chunks/s** | 38.4864 | **38.7202** | **+0.61 %** |
| **CPU-s per chunk** | 0.3544 | **0.3485** | **−1.67 %** ↓ better |
| **p95 latency (s)** | 252.709 | **247.967** | **−1.88 %** ↓ better |
| effective cores | 13.644 | 13.499 | −1.06 % — mechanism, not a win |
| CPU utilization | 56.83 % | 56.23 % | −1.06 % — mechanism, not a win |
| self-duplication | 0 / 988 | 0 / 988 | identical; gate passes both sides |

The patch moves the numbers in the direction its mechanism predicts, and the
most informative figure is not throughput but **CPU per chunk**. Effective cores
fell 1.06 % while throughput rose 0.61 % — the pipeline did *more* work using
*less* CPU. Measurement drift moves throughput and cores together; reclaimed
padding waste moves them apart, which is what happened.

> **Read this before quoting anything below.** Both cells ran once. There is no
> coefficient of variation, so every delta here is a single observation. The
> campaign's own calibration puts RocketRide default-posture repeatability at
> roughly **0.12 % pass-to-pass**, which makes +0.61 % and −1.67 % large enough
> to be worth confirming and too small to assert. Treat this as a sizing result
> that justifies a 3-repetition run, not as a finding.

---

## Environment

| component | value | notes |
|---|---|---|
| host | AWS EC2 `c7i.8xlarge` | 32 vCPU, 63.2 GiB RAM, gp3 storage, $1.428/h on-demand |
| instance | `i-0bdc8b1e18f2a5348` | access over SSM; auto-stops when idle |
| engine | RocketRide 3.3.1 | with the documented duplication correction — see Configuration |
| corpus | `~/bench_corpus_n10000_off200` | 10,026 govdocs PDFs staged, 6.0 GB; 1,000 measured + 25 warm drawn from it |
| corpus check | verified at run start | both cells ran the same files in the same order |
| isolation | unpinned | no cpuset; arms run sequentially, never concurrently |
| comparison arm | none | `SKIP_LG=1` — this is a RocketRide-vs-RocketRide probe |

The box sat idle before the run and returned to idle after it, so nothing else
competed for cores during either cell. Because threads were left unpinned and
only one repetition was taken, this environment produces sizing evidence rather
than publishable numbers.

---

## Configuration

The run used **RocketRide's default posture** deliberately — the question was
what PR #2197 does to a stock deployment, not what it does to a tuned one.

| setting | cell A — stock | cell B — patched |
|---|---|---|
| threads requested | `NONE` (engine default) | `NONE` (engine default) |
| tasks / pipelines | 1 | 1 |
| mode | blast | blast |
| documents | 1,000 + 25 warm | 1,000 + 25 warm |
| warm start (excluded) | 25 docs in 32.4 s | 25 docs in 31.6 s |
| `RR_DUP_PATCH` | 1 | 1 |
| `RR_LENSORT_PATCH` | **0** | **1** |
| image digest | `sha256:6ac4bd2d…` | `sha256:4d93cb37…` |
| window | 22:38:14 → 22:48:41Z | 22:49:10 → 22:58:41Z |

### Naming the tested system correctly

`RR_DUP_PATCH` defaults to `1` and was on in both cells. That is a **behaviour
patch, not a dependency pin**: in stock 3.3.1 the embedding-transformer node
calls `_flushDocuments()` and then also falls through to the default forwarding
action, emitting each document list twice — measured at 51 of 987 PDFs
returning `[A,B,C,A,B,C]`, inflating its work by about 40 %.

Describe the tested system as **"RocketRide 3.3.1 with the documented
embedding-transformer duplication correction"**, never as stock 3.3.1.

### The change under test (PR #2197)

One file, `ai/common/models/transformers/sentence_transformers.py`, in two
places. It sorts sentence indices by length before batching, then restores the
original order so embeddings still line up with their inputs:

```diff
+ order = sorted(range(len(sentences)), key=lambda i: len(sentences[i]))

  # Process in batches
- for i in range(0, len(sentences), batch_size):
-     batch = sentences[i : i + batch_size]
+ for i in range(0, len(order), batch_size):
+     indexes = order[i : i + batch_size]
+     batch = [sentences[index] for index in indexes]

  ...

- return np.array(all_embeddings)
+ restored = [None] * len(all_embeddings)
+ for position, index in enumerate(order):
+     restored[index] = all_embeddings[position]
+ return np.array(restored)
```

A batch is padded to the length of its longest member, so mixing a
40-character chunk with a 3,900-character one wastes compute on padding.
Grouping similar lengths together removes that waste — but only if the input
actually has length variance.

### How the A/B was enforced

The two cells differ in exactly one build argument. Because a patched-versus-stock
claim is worthless if the image is wrong, the patched image was checked three
independent ways:

- the image digest differs from cell A's — `4d93cb37…` vs `6ac4bd2d…`
- the image label `benchmark.rocketride.lensort_patch_pr2197` reads `1`
- a source grep **inside the running image** found all three patch sites: the
  sort at line 383, the index slice at 387, the order restoration at 425

The patch script is itself guarded: it asserts each anchor appears exactly once,
and re-reads and compiles the file after writing, so a silent partial
application fails the build rather than producing a plausible-looking image.

---

## Why PDFs respond where video did not

The same patch on the AMI video corpus produced **+0.08 %** — nothing. That was
not a failure of the patch; it was the corpus having no variance to exploit.
Every video chunk was a uniform 4,000-character block of detection JSON, and
MiniLM truncates at 512 tokens, so every chunk arrived at the model at exactly
the same width. Sorting a list whose elements are all the same length reorders
nothing.

PDFs are the opposite. Measured across 140 documents and 1,745 chunks from the
`pdf200` ground-truth records (`runs/pdf200/gt-rr/per_doc.jsonl`):

| chunks per document | min | p50 | p90 | p99 | max | mean |
|---|---:|---:|---:|---:|---:|---:|
| govdocs PDFs | 1 | 6 | 36 | 164 | 276 | 12.5 |

- **10.7 %** of documents exceed the 32-chunk batch size, so most documents
  never form more than one batch at all.
- But those few documents hold **57.6 %** of all chunks — so the majority of the
  embedding work *does* happen in multi-batch documents, which are exactly the
  ones sorting can help.

That distribution is why the PDF pipeline was the right place to retest a patch
video had already declared inert, and why a positive result here is coherent
rather than surprising.

---

## Evidence grade and known gaps

Both cells returned `M0 FAIL`. That is the evidence gate firing as designed, not
a crash — the runs completed and every number was kept. Three reasons, all
applying identically to both sides:

| gate | why it fired | effect on this comparison |
|---|---|---|
| determinism | needs ≥2 reps; we ran 1 by request | the reason these deltas cannot be asserted |
| census | worker census check on rep 1 | symmetric — same on both cells |
| provenance | missing `parser_config_hash`, `chunk_config`, `embedding_model` | blocks publication; does not bias A vs B |

### Two harness defects found during the run

- **The in-image verification read the wrong image.** The campaign's pre-build
  step runs without `CORPUS` in its environment, so Compose rejects the file
  with `invalid spec: :/corpus:ro:` and the build never happens there. The
  verification then greps whatever image the previous cell left behind — which
  is why cell B's log wrongly says `arrival-order batching (stock)`. The real
  build happens later inside `matched_run.sh`, where `CORPUS` is set. The three
  direct checks above were done by hand to cover this run; the script still
  needs fixing.
- **The auto-summary printed empty.** The metric-extraction regex fails to match
  the report format, so the campaign's own comparison table came out blank.
  Every number in this document was read directly from each cell's `report.txt`.

> **Not yet established:** that PR #2197 improves the PDF pipeline. What *is*
> established is that a single pair of runs moved four metrics in the predicted
> direction by more than the calibrated noise floor, with CPU-per-chunk and
> effective-cores moving in opposite directions in the way the mechanism requires.

---

## Pipeline surface area (lines of code)

Both arms run the same four compute stages and differ enormously in how much has
to be written by hand. Counts exclude Dockerfiles; "code" excludes blank lines,
comments and docstrings, with docstrings identified by parsing the AST rather
than by pattern-matching.

| arm / group | files | total | code | what it is |
|---|---:|---:|---:|---|
| **RocketRide — `pipe/benchmark_pdf.pipe`** | 1 | **125** | **125** | the entire authored artifact: 5 components, JSON |
| — semantic only | 1 | 78 | 78 | canvas `ui` blocks and editor prefs removed (47 lines of chrome) |
| — graph only | 1 | 71 | 71 | also dropping `name`, `project_id`, `version` |
| **LangGraph — pipeline definition** | 6 | 166 | **96** | graph, state, nodes, adapter |
| **LangGraph — workload** | 6 | 207 | **107** | extract, extract_pdf, split, embed |
| **LangGraph — service shell** | 9 | 617 | **428** | app, pipeline, registry, schemas, upload, canonical, config, errors |
| **LangGraph — total** | 21 | 990 | **631** | everything needed to stand the arm up |

### Node for node

| RocketRide component | provider | LangGraph node |
|---|---|---|
| `parse_1` | `parse` | `extract` |
| `preprocessor_1` | `preprocessor_langchain` | `chunk` |
| `embedding_1` | `embedding_transformer` | `embed` |
| `response_1` | `response_documents` | `assemble` |
| `webhook_1` | `webhook` | *not a node* — FastAPI layer + adapter |

### Which ratio to quote

- **631 vs 125 — 5.0×.** Everything you must write versus the whole RocketRide
  file. The honest "cost to build it yourself" figure.
- **203 vs 125 — 1.6×.** Graph plus workload only, because RocketRide gets HTTP
  transport, upload handling, config and error surface from the engine binary
  rather than from user code. **This is the one to lead with**, quoting the
  428-line shell separately.
- **203 vs 78 — 2.6×.** The same, against RocketRide's semantic lines with
  canvas chrome removed.

> **The caveat that outweighs the numbers.** This measures *authored* lines, not
> work done. RocketRide's `"profile": "miniLM"` is one line that resolves inside
> the engine to `multi-qa-MiniLM-L6-cos-v1`; the LangGraph side spends 39 lines
> saying the same thing explicitly. `parse_1` has an *empty* config and the
> engine supplies bundled Tika 3.2.3, while the LangGraph arm carries an
> `EXTRACTOR` switch plus a copy of RocketRide's own `tika-config.xml` so the
> parsers match. The engine code behind those declarations dwarfs either column.
> The defensible claim is about authoring effort and surface area under your
> control — not total system complexity.

---

## Next step

**Three repetitions per cell at the same N=1,000.** That yields a coefficient of
variation on each side and clears the determinism gate, which is what turns
+0.61 % into something quotable. On this run's roughly 10 minutes per cell, the
campaign costs about 40 minutes of box time.

Going to 10,000 documents instead would sharpen precision but costs roughly
3.5 hours and still leaves determinism unproven at one repetition. Repetitions
buy more here than corpus size does.

Two fixes should land before that run:

1. export `CORPUS` into the campaign's pre-build step so the in-image
   verification inspects the image it is about to run
2. repair the summary regex so the comparison table generates itself

Separately, the three provenance fields would need filling before any of this
could be published rather than circulated.

---

*Records: `results/pr2197pdf-stock-20260908T223814Z`,
`results/pr2197pdf-patched-20260908T223814Z`. Chunk distribution:
`runs/pdf200/gt-rr/per_doc.jsonl` (140 documents, 1,745 chunks). Line counts
measured 2026-09-09 against the working tree on branch `aws-bench`.*
