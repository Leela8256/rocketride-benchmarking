#!/usr/bin/env bash
# Phase 1 of the node-bottleneck investigation: attribute RocketRide's
# per-document CPU to a PIPELINE NODE, using the engine's own apaevt_flow
# traces (op=enter|leave per component), which have never been switched on
# correctly in this repo.
#
#   cell traced   RR_TRACE=summary  -> per-node wall time per document
#   cell control  RR_TRACE unset    -> the SAME driver, tracing off
#
# The control is not optional. Tracing rides the same websocket the driver
# measures on, so if it changes throughput the traced numbers describe a
# different run than the one we are trying to explain. Compare the two cells'
# docs/s and CPU/doc before quoting anything from the traced cell.
#
# RocketRide only (SKIP_LG=1), native untuned posture: threads unset, one task,
# one connection, PR #2197 off, duplication correction on as in every run since
# 16 Aug. Same corpus, order and envelope as the b16 baseline it is explained
# against.
#
#   bash run/nodeprofile.sh                 # both cells, N=1000
#   N=200 bash run/nodeprofile.sh           # quicker shakedown
#   CELLS=traced bash run/nodeprofile.sh    # one cell only
set -uo pipefail
cd "$(dirname "$0")/.."
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
CELLS="${CELLS:-traced control}"
export N="${N:-1000}" REPS="${REPS:-1}" WARM="${WARM:-24}" CORPUS_OFFSET="${CORPUS_OFFSET:-200}"
export MODE="${MODE:-b16}"
export SKIP_LG=1 CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-report}"
export MAX_ARCHIVES="${MAX_ARCHIVES:-50}"
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-3600}"
export RR_LENSORT_PATCH=0 RR_DUP_PATCH="${RR_DUP_PATCH:-1}"
unset RR_THREADS RR_POOL_MAX
export BENCH_S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench/nodeprofile-$DATE}"
say() { echo "[nodeprofile $(date -u +%H:%M:%S)] $*"; }

# Keepalive on the last host core, outside the arm's cpuset (0-23), so it can
# never land in the measured engine cgroup. Same disclosure as every campaign.
taskset -c "$(( $(nproc) - 1 ))" bash -c 'while :; do :; done' &
KEEP=$!
trap 'kill $KEEP 2>/dev/null' EXIT

say "campaign $STAMP  cells=[$CELLS]  N=$N mode=$MODE offset=$CORPUS_OFFSET  S3=$BENCH_S3"
for CELL in $CELLS; do
  say "=============== cell $CELL ==============="
  if [ "$CELL" = "traced" ]; then export RR_TRACE=summary; else unset RR_TRACE; fi
  RUN_TAG="nodeprofile-${CELL}-${STAMP}" bash run/matched_run.sh < /dev/null
  say "cell $CELL finished rc=$? (M0 FAIL at 1 rep is expected — determinism unproven)"
done
unset RR_TRACE

say "=== SUMMARY $STAMP ==="
python3 - "$STAMP" $CELLS <<'PY'
import json, os, sys
stamp, cells = sys.argv[1], sys.argv[2:]
rows = {}
for c in cells:
    d = f"results/nodeprofile-{c}-{stamp}"
    f = os.path.join(d, "RUN_REPORT.json")
    if not os.path.exists(f):
        print(f"  {c}: no RUN_REPORT.json"); continue
    j = json.load(open(f)); a = (j["arms"].get("rocketride") or {})
    rep = (a.get("reps") or [{}])[0]
    t, m, e = rep.get("m1_throughput", {}), rep.get("m7_resources", {}), rep.get("m7_efficiency", {})
    rows[c] = {"docs_per_s": t.get("docs_per_s"), "chunks_per_s": t.get("chunks_per_s"),
               "span_s": t.get("window_span_s"), "cores": m.get("effective_cores"),
               "cpu_per_doc": e.get("cpu_seconds_per_doc"),
               "cpu_per_chunk": e.get("cpu_seconds_per_chunk"),
               "ok": rep.get("census", {}).get("completed")}
    meta = rep.get("meta", {})
    rows[c]["trace_level"] = meta.get("trace_level")
    rows[c]["flow_ops"] = meta.get("flow_ops_seen")
    rows[c]["node_timings"] = meta.get("node_timings")

print(f"{'cell':<10}{'ok':>7}{'docs/s':>9}{'chunks/s':>10}{'span s':>9}{'cores':>8}{'cpu/doc':>9}{'cpu/chunk':>11}  trace")
for c, v in rows.items():
    print(f"{c:<10}{v['ok'] or 0:>7}{v['docs_per_s'] or 0:>9.3f}{v['chunks_per_s'] or 0:>10.2f}"
          f"{v['span_s'] or 0:>9.0f}{v['cores'] or 0:>8.2f}{v['cpu_per_doc'] or 0:>9.3f}"
          f"{v['cpu_per_chunk'] or 0:>11.4f}  {v['trace_level']}")

# THE control check: does tracing change the run it is measuring?
if "traced" in rows and "control" in rows:
    print("\nPERTURBATION CONTROL (traced vs control):")
    for k in ("docs_per_s", "chunks_per_s", "cpu_per_doc", "cores"):
        a, b = rows["control"].get(k), rows["traced"].get(k)
        if a and b:
            d = 100 * (b / a - 1)
            flag = "  <-- TRACING PERTURBS; traced node shares are indicative only" if abs(d) > 5 else ""
            print(f"  {k:<14} control {a:>10.4f}   traced {b:>10.4f}   {d:+6.2f}%{flag}")

nt = (rows.get("traced") or {}).get("node_timings")
if nt and nt.get("nodes"):
    print(f"\nPER-NODE (basis: {nt['basis']})")
    print(f"  nested={nt['nested']} depth={nt['max_nesting_depth']} "
          f"unmatched_leave={nt['unmatched_leave']} unclosed_enter={nt['unclosed_enter']}")
    print(f"  {'node':<26}{'calls':>8}{'total s':>10}{'% node':>8}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>11}")
    for nm, v in nt["nodes"].items():
        print(f"  {nm:<26}{v['calls']:>8}{v['total_s']:>10.1f}{v['pct_of_node_time']:>8.1f}"
              f"{v['p50_ms']:>10.1f}{v['p95_ms']:>10.1f}{v['max_ms']:>11.1f}")
elif "traced" in rows:
    print("\nNO NODE TIMINGS: flow traces did not arrive. "
          "Fall through to Phase 2 (truncated-pipe ablation).")
PY
touch "results/nodeprofile-CAMPAIGN-DONE-${STAMP}"
say "=== CAMPAIGN COMPLETE $STAMP ==="
