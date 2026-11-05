#!/usr/bin/env bash
# Build PAIRED RocketRide-vs-LangGraph reports for the batch-submission
# campaign, one directory per batch size.
#
# The two arms were measured days apart (RocketRide 2026-09-16, LangGraph
# 2026-09-21) on the same box, corpus, document order, envelope and batch
# sizes, one repetition each. Neither source run is modified: this assembles
# COPIES so bench/report.py can produce the cross-arm section (byte parity,
# chunk ratio, workload ratio) that a single-arm run cannot have.
#
# It also carries each arm's determinism evidence across. Each source campaign
# proved determinism by BATCH-SIZE INVARIANCE (b16 vs b32 vs b64, cyclically)
# and recorded it in that run's CROSS_MODE_DETERMINISM.json -- but each file
# names only its own arm, the other reading "missing per_doc.jsonl". Merging
# the two arms' verdicts into the paired directory is what lets the paired
# report reach a real verdict instead of failing on a determinism question
# both campaigns already answered.
#
#   bash run/batch10k_pair_reports.sh <LG_STAMP> [RR_STAMP] [S3 prefix]
set -uo pipefail
cd "$(dirname "$0")/.."
LG_STAMP="${1:?LangGraph campaign stamp}"
RR_STAMP="${2:-20260916T065635Z}"
S3="${3:-s3://rocketride-benchmark-data/leela/bench/batch10k-lg-2026-09-21}"
SIZES="${SIZES:-16 32 64}"
export CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-report}"

for B in $SIZES; do
  LGR="results/batch10k-lg-b${B}-${LG_STAMP}"
  RRR="results/batch10k-b${B}-${RR_STAMP}"
  P="results/batch10k-paired-b${B}-${LG_STAMP}"
  echo "=== paired b$B"
  if [ ! -f "$LGR/lg/rep1/per_doc.jsonl" ] || [ ! -f "$RRR/rr/rep1/per_doc.jsonl" ]; then
    echo "  skip: missing $( [ -f "$LGR/lg/rep1/per_doc.jsonl" ] || echo "$LGR/lg" ) $( [ -f "$RRR/rr/rep1/per_doc.jsonl" ] || echo "$RRR/rr" )"
    continue
  fi
  rm -rf "$P"; mkdir -p "$P"
  cp -r "$LGR/lg" "$P/lg"; cp -r "$RRR/rr" "$P/rr"
  for f in environment.txt corpus.sha256 corpus_manifest.json meta_lg.json pip_freeze_lg.txt; do
    [ -f "$LGR/$f" ] && cp "$LGR/$f" "$P/$f"
  done
  [ -f "$RRR/engine_sha.txt" ] && cp "$RRR/engine_sha.txt" "$P/"
  cat "$LGR/image_ids.txt" "$RRR/image_ids.txt" 2>/dev/null | sort -u > "$P/image_ids.txt"

  python3 - "$P" "$LGR" "$RRR" "$B" "$LG_STAMP" "$RR_STAMP" <<'PY'
import json, sys, os
P, LGR, RRR, B, LGS, RRS = sys.argv[1:7]

def arm_verdict(run, arm):
    f = os.path.join(run, "CROSS_MODE_DETERMINISM.json")
    if not os.path.exists(f):
        return None, None
    d = json.load(open(f))
    return (d.get("arms") or {}).get(arm), (d.get("run_a"), d.get("run_b"))

lg, lgpair = arm_verdict(LGR, "langgraph")
rr, rrpair = arm_verdict(RRR, "rocketride")
merged = {
    "run_a": os.path.basename(P),
    "run_b": "assembled from each arm's own batch-size-invariance comparison",
    "note": ("Each arm's verdict is carried over from its own campaign, where "
             "determinism was established by BATCH-SIZE INVARIANCE across the "
             "b16/b32/b64 cells (cyclic comparison). Nothing was re-derived here."),
    "sources": {"langgraph": {"run": os.path.basename(LGR), "compared_against": lgpair},
                "rocketride": {"run": os.path.basename(RRR), "compared_against": rrpair}},
    "arms": {k: v for k, v in (("langgraph", lg), ("rocketride", rr)) if v},
}
json.dump(merged, open(os.path.join(P, "CROSS_MODE_DETERMINISM.json"), "w"), indent=1)
for name, v in merged["arms"].items():
    print(f"  carried {name}: {v.get('identical')}/{v.get('compared')} identical "
          f"({v.get('mode_a')} vs {v.get('mode_b')}) PASS={v.get('PASS')}")
if len(merged["arms"]) < 2:
    print("  WARNING: only one arm had a determinism verdict to carry")

open(os.path.join(P, "PAIRING.md"), "w").write(f"""# Paired report — batch size {B}

**Assembled, not run.** The two arms were measured on different days on the same
box, from the same corpus (see `corpus.sha256`), in the same document order,
under the same 24-core envelope, at the same batch size {B}, one repetition each.

| arm | source run | measured |
|---|---|---|
| RocketRide | `batch10k-b{B}-{RRS}` | 2026-09-16 |
| LangGraph | `batch10k-lg-b{B}-{LGS}` | 2026-09-21 |

`environment.txt` here is the LangGraph run's. The RocketRide records are copied
verbatim and **neither source directory is modified**. Every per-arm figure is
identical to the one in its source run; what this directory adds is the
**cross-arm section** — byte parity, chunk ratio, workload ratio — which a
single-arm run cannot produce.

`CROSS_MODE_DETERMINISM.json` here is a merge of the two campaigns' own
verdicts, each established by batch-size invariance across that campaign's
b16/b32/b64 cells. Nothing was re-derived during assembly.

## The submission semantics differ by arm and must be quoted that way

- **RocketRide** sends ONE `send_files` call carrying {B} files; the engine
  schedules them internally.
- **LangGraph** has no batch endpoint, so a wave is {B} concurrent HTTP
  requests released together and joined at a barrier.

Same offered load shape — at most {B} in flight, next wave only after the
slowest member of this one — different wire protocol. Per-document time is
measured from wave release to that document's own completion on both arms, so
both carry intra-wave queueing and both are labelled batch-position latency.

LangGraph's service executor is LangGraph's default, width
`min(32, cpu_count+4)` = 32 on this box, so a wave of 64 cannot be 64 wide
inside the service. Read `achieved concurrency`, not the offered number.
""")
PY

  # The paired directory is a run record in its own right and must carry its
  # own provenance, or report.py fails it for a missing file rather than for
  # anything about the measurement. Derived from the assembled environment:
  # the LangGraph run's environment.txt and /meta, the RocketRide run's engine
  # hash, both image ids.
  python3 run/write_provenance.py "$P" | sed 's/^/  /'
  python3 bench/report.py "$P" > "$P/report.txt" 2>&1
  grep -E '^RUN (PASS|FAIL)|reason:' "$P/report.txt" | head -4
  sed -n '/^cross-arm/,+3p' "$P/report.txt" | head -4
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$P/" "$S3/$(basename "$P")/" --recursive --only-show-errors \
      && echo "  uploaded -> $S3/$(basename "$P")/"
  fi
done
