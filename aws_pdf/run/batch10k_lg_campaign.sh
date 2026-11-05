#!/usr/bin/env bash
# LangGraph arm of the batch-submission campaign — the companion to
# run/batch10k_campaign.sh, which measured RocketRide at b16/b32/b64 on
# 2026-09-16.
#
#   cells    b16, b32, b64 on the SAME 10,000 GovDocs1 PDFs, same order, same
#            24-core envelope, same 24 disjoint warm-up docs, 1 rep each.
#   arm      LangGraph only (SKIP_RR=1); Tika 3.2.3 sidecar carrying
#            RocketRide's own tika-config, on the arm's cpuset, its CPU added
#            to LangGraph's cost by bench/report.py.
#   meaning  b<B> for LangGraph is consecutive WAVES of B concurrent HTTP
#            requests joined at a barrier. It is NOT the same wire protocol as
#            RocketRide's single batched send_files call -- LangGraph has no
#            batch endpoint -- but it offers the same load shape: at most B in
#            flight, next wave only after the slowest member of this one.
#            Never describe the two as the same submission interface.
#   caveat   the service's executor is LangGraph's default, width
#            min(32, cpu_count+4) = 32 here, so a wave of 64 cannot be 64-wide
#            inside the service. `achieved concurrency` in each report is the
#            number that matters; `max_inflight_requests` on /meta is inert.
#
#   bash run/batch10k_lg_campaign.sh                 # all three cells
#   SIZES="16" bash run/batch10k_lg_campaign.sh      # a subset
set -uo pipefail
cd "$(dirname "$0")/.."
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
SIZES="${SIZES:-16 32 64}"
# The RocketRide cells to pair each LangGraph cell against for the cross-arm
# section. Empty = skip pairing.
RR_STAMP="${RR_STAMP:-20260916T065635Z}"
export N="${N:-10000}" REPS="${REPS:-1}" WARM="${WARM:-24}" CORPUS_OFFSET="${CORPUS_OFFSET:-200}"
export SKIP_RR=1 CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-report}"
export MAX_ARCHIVES="${MAX_ARCHIVES:-50}"
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-3600}"
export LG_EXTRACTOR="${LG_EXTRACTOR:-tika}"
export BENCH_S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench/batch10k-lg-$DATE}"
say() { echo "[lg-campaign $(date -u +%H:%M:%S)] $*"; }

# Idle-watchdog keepalive on the LAST host core, outside the arm's cpuset
# (0-23), so it never lands in a measured cgroup. Same disclosure as the
# RocketRide campaign.
taskset -c "$(( $(nproc) - 1 ))" bash -c 'while :; do :; done' &
KEEP=$!
trap 'kill $KEEP 2>/dev/null' EXIT

say "campaign $STAMP  date=$DATE  cells=[$SIZES]  N=$N warm=$WARM offset=$CORPUS_OFFSET"
say "S3=$BENCH_S3   pairing against RocketRide stamp ${RR_STAMP:-<none>}"
for B in $SIZES; do
  say "=============== cell lg-b$B ==============="
  RUN_TAG="batch10k-lg-b${B}-${STAMP}" MODE="b${B}" bash run/matched_run.sh < /dev/null
  say "cell lg-b$B finished rc=$? (FAIL at 1 rep is expected until the cross-cell step)"
done

runs=(); for B in $SIZES; do runs+=("results/batch10k-lg-b${B}-${STAMP}"); done
n=${#runs[@]}
if [ "$n" -ge 2 ]; then
  say "cross-cell determinism (each cell vs the next, wrapping)"
  for i in $(seq 0 $(( n - 1 ))); do
    a="${runs[$i]}"; b="${runs[$(( (i + 1) % n ))]}"
    [ -f "$a/lg/rep1/per_doc.jsonl" ] && [ -f "$b/lg/rep1/per_doc.jsonl" ] || { say "skip $a vs $b"; continue; }
    python3 bench/cross_mode_determinism.py "$a" "$b" | tail -5
  done
  say "regenerating reports with the determinism basis, re-uploading"
  for r in "${runs[@]}"; do
    [ -d "$r" ] || continue
    python3 bench/report.py "$r" > "$r/report.txt" 2>&1
    grep -E '^RUN (PASS|FAIL)|reason:' "$r/report.txt" | head -4
    aws s3 cp "$r/" "$BENCH_S3/$(basename "$r")/" --recursive --only-show-errors \
      && say "exfil OK -> $BENCH_S3/$(basename "$r")/"
  done
fi

# ---- paired cross-arm reports (best effort; never touches either source) ----
if [ -n "$RR_STAMP" ]; then
  say "building paired RR-vs-LG reports"
  for B in $SIZES; do
    LGR="results/batch10k-lg-b${B}-${STAMP}"; RRR="results/batch10k-b${B}-${RR_STAMP}"
    P="results/batch10k-paired-b${B}-${STAMP}"
    [ -f "$LGR/lg/rep1/per_doc.jsonl" ] && [ -f "$RRR/rr/rep1/per_doc.jsonl" ] || { say "skip pair b$B (missing arm)"; continue; }
    rm -rf "$P"; mkdir -p "$P"
    cp -r "$LGR/lg" "$P/lg"; cp -r "$RRR/rr" "$P/rr"
    for f in environment.txt corpus.sha256 corpus_manifest.json image_ids.txt meta_lg.json; do
      [ -f "$LGR/$f" ] && cp "$LGR/$f" "$P/$f"
    done
    [ -f "$RRR/engine_sha.txt" ] && cp "$RRR/engine_sha.txt" "$P/"
    cat > "$P/PAIRING.md" <<PAIR
# Paired report — batch size $B

Assembled, not run. The two arms were measured on different days on the same
box, same corpus (manifest sha in corpus.sha256), same document order, same
24-core envelope, same batch size $B, one repetition each.

| arm | source run | measured |
|---|---|---|
| RocketRide | \`batch10k-b${B}-${RR_STAMP}\` | 2026-09-16 |
| LangGraph  | \`batch10k-lg-b${B}-${STAMP}\` | $DATE |

environment.txt here is the LangGraph run's. The RocketRide records are copied
verbatim; neither source directory is modified. Throughput and cost figures per
arm are identical to those in the source runs -- the only thing this directory
adds is the CROSS-ARM section (byte parity, chunk ratio, workload ratio).

Submission semantics differ by arm and must be quoted that way: RocketRide sends
ONE send_files call carrying $B files; LangGraph sends $B concurrent HTTP
requests joined at a barrier. Same offered load shape, different wire protocol.
PAIR
    python3 bench/report.py "$P" > "$P/report.txt" 2>&1
    echo "  b$B paired: $(grep -E '^cross-arm' -A2 "$P/report.txt" | head -3 | tr '\n' ' ')"
    aws s3 cp "$P/" "$BENCH_S3/$(basename "$P")/" --recursive --only-show-errors \
      && say "exfil OK -> $BENCH_S3/$(basename "$P")/"
  done
fi

say "=== SUMMARY $STAMP ==="
python3 - "${runs[@]}" <<'PY'
import json, sys, os
print(f"{'cell':<10}{'ok':>11}{'chunks':>9}{'docs/s':>9}{'chunks/s':>10}{'span s':>9}{'cores':>8}{'util':>7}{'cpu/doc':>9}{'cpu/chunk':>11}{'p50':>8}{'p95':>8}{'max':>9}{'conc':>7}  verdict")
for r in sys.argv[1:]:
    f = os.path.join(r, "RUN_REPORT.json")
    if not os.path.exists(f):
        print(f"{os.path.basename(r):<10} (no RUN_REPORT.json)"); continue
    j = json.load(open(f)); a = j["arms"].get("langgraph") or {}
    rep = (a.get("reps") or [{}])[0]; c = rep.get("census", {}); t = rep.get("m1_throughput", {})
    l = rep.get("m2_latency", {}); e = rep.get("m7_efficiency", {}); m = rep.get("m7_resources", {})
    cc = rep.get("concurrency", {})
    cell = "lg-" + os.path.basename(r).split("-")[2]
    print(f"{cell:<10}{str(c.get('completed'))+'/'+str(c.get('offered')):>11}{t.get('successful_chunks') or 0:>9}{t.get('docs_per_s') or 0:>9.3f}{t.get('chunks_per_s') or 0:>10.2f}{t.get('window_span_s') or 0:>9.0f}{m.get('effective_cores') or 0:>8.2f}{(e.get('cpu_utilization') or 0):>7.2f}{e.get('cpu_seconds_per_doc') or 0:>9.3f}{e.get('cpu_seconds_per_chunk') or 0:>11.4f}{l.get('p50') or 0:>8.1f}{l.get('p95') or 0:>8.1f}{l.get('max') or 0:>9.1f}{cc.get('achieved_mean_time_weighted') or 0:>7.1f}  M0={'PASS' if a.get('m0_PASS') else 'FAIL'} det={a.get('determinism_PASS')} prov={'ok' if (j.get('provenance') or {}).get('PASS') else 'INCOMPLETE'}")
PY
touch "results/batch10k-lg-CAMPAIGN-DONE-${STAMP}"
say "=== CAMPAIGN COMPLETE $STAMP ==="
