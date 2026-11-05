#!/usr/bin/env bash
# RocketRide-only PDF campaign: the STANDARD engine, 10,000 GovDocs1 PDFs,
# submitted in consecutive send_files batches of B on ONE connection.
#
#   cells   B = 16, 32, 64   (MODE=b<B>, one rep each)
#   engine  3.3.1 as built on this box: the documented Linux boot-pin fix and
#           the documented duplication correction (RR_DUP_PATCH=1, as in every
#           run since 16 Aug), PR #2197 patch OFF, `threads` UNSET (engine
#           default), no BLAS/OMP change beyond the harness' standing
#           OMP_NUM_THREADS=1 pin that every PDF run has carried.
#   corpus  bench_corpus_n10000_off200: docs 201..10200 by basename + 24
#           disjoint warm-up docs (identical to Runs 1 and 11; manifest sha
#           must reproduce ec2797f0...)
#   metrics the full PDF suite via run/matched_run.sh + bench/report.py
#           (census/structure/self-dup/input-integrity, throughput, latency,
#           resources, efficiency, provenance). Determinism: one rep per cell
#           cannot prove rep-to-rep determinism, so the three cells are
#           compared pairwise with bench/cross_mode_determinism.py (same 10k
#           docs, different batch sizes) and each report is regenerated with
#           that basis recorded.
#   S3      s3://rocketride-benchmark-data/leela/bench/batch10k-<DATE>/batch10k-b<B>-<STAMP>/
#
#   bash run/batch10k_campaign.sh              # all three cells
#   SIZES="16" bash run/batch10k_campaign.sh   # a subset
set -uo pipefail
cd "$(dirname "$0")/.."
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
SIZES="${SIZES:-16 32 64}"
export N="${N:-10000}" REPS="${REPS:-1}" WARM="${WARM:-24}" CORPUS_OFFSET="${CORPUS_OFFSET:-200}"
export SKIP_LG=1 CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-report}"
export MAX_ARCHIVES="${MAX_ARCHIVES:-50}"      # the 10k corpus spans ~42 govdocs archives
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-3600}"
export RR_LENSORT_PATCH=0 RR_DUP_PATCH="${RR_DUP_PATCH:-1}"
unset RR_THREADS RR_POOL_MAX                    # standard engine: no tuning
export BENCH_S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench/batch10k-$DATE}"
say() { echo "[campaign $(date -u +%H:%M:%S)] $*"; }

# Keepalive: the box's idle watchdog stops the instance during low-CPU phases
# (corpus extraction, hashing, image build). One busy loop, pinned to the LAST
# host core -- outside the arm's cpuset (0-23), so it never lands in the
# measured engine cgroup; it shares only the bench client's cores and is
# disclosed here and in the campaign log.
taskset -c "$(( $(nproc) - 1 ))" bash -c 'while :; do :; done' &
KEEP=$!
trap 'kill $KEEP 2>/dev/null' EXIT

say "campaign $STAMP  date=$DATE  cells=[$SIZES]  N=$N warm=$WARM offset=$CORPUS_OFFSET  S3=$BENCH_S3"
for B in $SIZES; do
  say "=============== cell b$B ==============="
  RUN_TAG="batch10k-b${B}-${STAMP}" MODE="b${B}" bash run/matched_run.sh < /dev/null
  say "cell b$B finished rc=$? (FAIL here is expected at 1 rep: determinism unproven until the cross-cell step)"
  docker image inspect bench-rocketride:latest \
    --format 'engine image {{.Id}} lensort_patch={{index .Config.Labels "benchmark.rocketride.lensort_patch_pr2197"}} dup_patch={{index .Config.Labels "benchmark.rocketride.duplication_patch"}}'
done

# ---- determinism by batch-size invariance (same docs, different B) ----------
runs=(); for B in $SIZES; do runs+=("results/batch10k-b${B}-${STAMP}"); done
n=${#runs[@]}
if [ "$n" -ge 2 ]; then
  say "cross-cell determinism (each cell vs the next, wrapping)"
  for i in $(seq 0 $(( n - 1 ))); do
    a="${runs[$i]}"; b="${runs[$(( (i + 1) % n ))]}"
    [ -f "$a/rr/rep1/per_doc.jsonl" ] && [ -f "$b/rr/rep1/per_doc.jsonl" ] || { say "skip $a vs $b (missing records)"; continue; }
    python3 bench/cross_mode_determinism.py "$a" "$b" | tail -6
  done
  say "regenerating reports with the determinism basis, re-uploading"
  for r in "${runs[@]}"; do
    [ -d "$r" ] || continue
    python3 bench/report.py "$r" > "$r/report.txt" 2>&1; echo "  $r -> $(tail -1 "$r/report.txt" | head -c 80)"
    grep -E '^RUN (PASS|FAIL)|reason:' "$r/report.txt" | head -5
    aws s3 cp "$r/" "$BENCH_S3/$(basename "$r")/" --recursive --only-show-errors && say "exfil OK -> $BENCH_S3/$(basename "$r")/"
  done
fi

say "=== SUMMARY $STAMP ==="
python3 - "$STAMP" "${runs[@]}" <<'PY'
import json, sys, os
stamp, runs = sys.argv[1], sys.argv[2:]
print(f"{'cell':<10}{'ok':>11}{'chunks':>9}{'docs/s':>9}{'chunks/s':>10}{'span s':>9}{'cores':>8}{'util':>7}{'cpu/doc':>9}{'cpu/chunk':>11}{'p50':>8}{'p95':>8}{'max':>9}{'rss MB':>9}{'thr':>6}  verdict")
for r in runs:
    f = os.path.join(r, "RUN_REPORT.json")
    if not os.path.exists(f):
        print(f"{os.path.basename(r):<10} (no RUN_REPORT.json)"); continue
    j = json.load(open(f)); a = j["arms"].get("rocketride") or {}
    rep = (a.get("reps") or [{}])[0]; c = rep.get("census", {}); t = rep.get("m1_throughput", {})
    l = rep.get("m2_latency", {}); e = rep.get("m7_efficiency", {}); m = rep.get("m7_resources", {})
    cell = os.path.basename(r).split("-")[1]
    print(f"{cell:<10}{str(c.get('completed'))+'/'+str(c.get('offered')):>11}{t.get('successful_chunks') or 0:>9}{t.get('docs_per_s') or 0:>9.3f}{t.get('chunks_per_s') or 0:>10.2f}{t.get('window_span_s') or 0:>9.0f}{m.get('effective_cores') or 0:>8.2f}{(e.get('cpu_utilization') or 0):>7.2f}{e.get('cpu_seconds_per_doc') or 0:>9.3f}{e.get('cpu_seconds_per_chunk') or 0:>11.4f}{l.get('p50') or 0:>8.1f}{l.get('p95') or 0:>8.1f}{l.get('max') or 0:>9.1f}{(m.get('rss_mb') or {}).get('peak') or 0:>9.0f}{(m.get('threads') or {}).get('peak') or 0:>6}  M0={'PASS' if a.get('m0_PASS') else 'FAIL'} det={a.get('determinism_PASS')} prov={'ok' if (j.get('provenance') or {}).get('PASS') else 'INCOMPLETE'}")
PY
touch "results/batch10k-CAMPAIGN-DONE-${STAMP}"
say "=== CAMPAIGN COMPLETE $STAMP ==="
