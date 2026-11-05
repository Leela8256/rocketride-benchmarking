#!/usr/bin/env bash
# Phase 3: attribute the catastrophic documents to a NODE, one document at a time.
#
# Five documents that RocketRide takes 3 to 28 minutes on and LangGraph parses
# in ~1 s, plus controls, run SEQUENTIALLY with per-node tracing on. Sequential
# is the point: with one document in flight there is no queueing to blame and
# no ambiguity about which pipe belongs to which file -- every flow event falls
# inside exactly one document's [submit, completion] interval, on the same
# clock, so attribution is arithmetic rather than inference.
#
# What this settles. Both arms run Apache Tika 3.2.3, and the engine's own
# java/tika-config.xml is materially identical to the sidecar's the LangGraph
# arm uses -- both exclude TesseractOCRParser, same PDFParser params (the
# engine additionally excludes GDALParser). So if parse_1's SELF time accounts
# for the minutes, the cost is in the engine's plumbing around Tika (the JNI
# stream path, the mutex-guarded callback queue, the per-instance ping-pong
# thread), not in Tika, its version, or its configuration.
#
#   bash run/parse_pathology.sh
set -uo pipefail
cd "$(dirname "$0")/.."
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
SRC="${SRC:-$HOME/bench_corpus_n10000_off200}"
CUR="${CUR:-$HOME/bench_corpus_pathology}"

# Sorted order matters: the driver takes all_pdfs[:N] as measured and the next
# WARM as warm-up, so the measured set must sort first and the warm files after.
MEASURED="001029.pdf 001030.pdf 011464.pdf 011730.pdf 012852.pdf 039660.pdf"
#   001029  3.01 MB             <- SIZE-MATCHED control for 039660 (3.16 MB). If a
#           3 MB ordinary PDF parses in seconds while a 3 MB near-empty one takes
#           28 minutes, file size is eliminated as the variable.
#   001030  0.05 MB             <- floor: what a trivial document costs
#   011464  1.25 MB, 3 chunks   RR 725 s    LG 0.99 s
#   011730  1.15 MB, 7 chunks   RR 335 s    LG 1.10 s
#   012852  1.83 MB, 1959 chunks RR 232 s   LG 217 s   <- text-heavy anchor: the one
#           document where the two arms AGREE. A clean result shows the pathology
#           on the near-empty files and NOT on this one.
#   039660  3.16 MB, 3 chunks   RR 1677 s   LG 1.73 s  <- the 970x case
N_MEASURED=6
WARM_N=2

[ -d "$SRC" ] || { echo "FATAL: source corpus $SRC not found"; exit 1; }
if [ ! -f "$CUR/corpus_manifest.json" ]; then
  echo "building curated corpus at $CUR"
  rm -rf "$CUR"; mkdir -p "$CUR"
  for f in $MEASURED; do
    [ -f "$SRC/$f" ] || { echo "FATAL: $f missing from $SRC"; exit 1; }
    cp "$SRC/$f" "$CUR/"
  done
  # Warm files: the lexicographically LAST files in the source, so they sort
  # after every measured file and the driver's slice lands where intended.
  WARM_FILES=$(ls "$SRC"/*.pdf | xargs -n1 basename | sort | tail -n "$WARM_N")
  for f in $WARM_FILES; do cp "$SRC/$f" "$CUR/"; done
  ( cd "$CUR" && sha256sum ./*.pdf > SHA256SUMS && sha256sum -c --quiet SHA256SUMS )
  python3 - "$CUR" "$N_MEASURED" "$MEASURED" "$WARM_FILES" <<'PY'
import json, sys, pathlib
dest, n, measured, warm = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3].split(), sys.argv[4].split()
(dest / "corpus_manifest.json").write_text(json.dumps({
    "corpus": "govdocs1", "source": "curated subset of bench_corpus_n10000_off200",
    "selection_rule": "documents where RocketRide's cost is pathological, plus controls",
    "n_measured": n, "n_warm_extra": len(warm), "offset": 200,
    "docs_measured": sorted(measured), "docs_warm": sorted(warm)}, indent=1))
PY
fi
echo "curated corpus: $(ls "$CUR"/*.pdf | wc -l) pdfs"
ls -la "$CUR"/*.pdf | awk '{printf "  %-16s %8.2f MB\n", $9, $5/1048576}'

export CORPUS="$CUR" N="$N_MEASURED" WARM="$WARM_N" REPS=1 MODE=seq
export SKIP_LG=1 CENSUS_EMPTY_POLICY=report
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-7200}"
export RR_TRACE=summary RR_LENSORT_PATCH=0 RR_DUP_PATCH="${RR_DUP_PATCH:-1}"
unset RR_THREADS RR_POOL_MAX
export BENCH_S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench/nodeprofile-$DATE}"

taskset -c "$(( $(nproc) - 1 ))" bash -c 'while :; do :; done' &
KEEP=$!; trap 'kill $KEEP 2>/dev/null' EXIT

RUN_TAG="pathology-${STAMP}" bash run/matched_run.sh < /dev/null
echo "[pathology] run finished rc=$? (M0 FAIL at 1 rep expected)"

R="results/pathology-${STAMP}/rr/rep1"
python3 - "$R" <<'PY'
import json, collections, os, sys
R = sys.argv[1]
rows = [json.loads(l) for l in open(os.path.join(R, "per_doc.jsonl")) if l.strip()]
docs = [r for r in rows if not r.get("kind")]
ev = [json.loads(l) for l in open(os.path.join(R, "flow_events.jsonl")) if l.strip()]

# Exclusive self time per (document, node). Sequential mode means every flow
# event falls inside exactly one document's [submit, completion] window, on the
# same perf_counter clock -- so attribution needs no id mapping.
spans = sorted((d["submit_ns"], d["completion_ns"], d["doc"]) for d in docs)
def owner(ts):
    for a, b, name in spans:
        if a <= ts <= b:
            return name
    return None

stacks = collections.defaultdict(list)
self_by = collections.defaultdict(lambda: collections.defaultdict(float))
for e in ev:
    p, op, c, ts = e["pipe"], e["op"], e["component"], e["ts_ns"]
    if op == "enter":
        stacks[p].append([c, ts, 0])
    elif op == "leave" and stacks[p]:
        comp, t0, child = stacks[p].pop()
        dur = ts - t0
        who = owner(t0)
        if who:
            self_by[who][comp] += (dur - child) / 1e9
        if stacks[p]:
            stacks[p][-1][2] += dur

print()
print("PER-DOCUMENT NODE SELF TIME (seconds), sequential, one document in flight")
print("%-14s %9s %8s %10s %10s %10s %10s %10s" % (
    "doc", "total s", "chunks", "parse", "preproc", "embed", "response", "unattrib"))
for d in sorted(docs, key=lambda r: -(r["completion_ns"] - r["submit_ns"])):
    tot = (d["completion_ns"] - d["submit_ns"]) / 1e9
    s = self_by.get(d["doc"], {})
    acc = sum(s.values())
    print("%-14s %9.1f %8s %10.1f %10.2f %10.1f %10.2f %10.1f" % (
        d["doc"], tot, d.get("n_chunks"), s.get("parse_1", 0), s.get("preprocessor_1", 0),
        s.get("embedding_1", 0), s.get("response_1", 0), tot - acc))
print()
print("unattrib = document wall time not inside any traced node call")
print("           (engine transport, spooling, scheduling, result assembly)")
PY
