#!/usr/bin/env bash
# Post-campaign finalisation for run/batch10k_campaign.sh cells.
#
# The runner's in-container sampler was capped at 7200 s (fixed since), so
# cells longer than two hours lost M7 coverage. A box-side supervisor ran the
# same sampler uncapped per engine container into ~/logs/sampler_full_*.jsonl.
# For every cell: pick the supplementary streams whose timestamps fall inside
# the cell's measured window (+30 s), merge them (bench/merge_samplers.py,
# original kept), regenerate the report (with the campaign's cross-cell
# CROSS_MODE_DETERMINISM.json already in place), re-upload, and print a
# summary table.
#
#   bash run/batch10k_finalize.sh <STAMP> [S3 prefix] [sampler dir]
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="${1:?campaign stamp, e.g. 20260916T065635Z}"
S3="${2:-s3://rocketride-benchmark-data/leela/bench/batch10k-2026-09-15}"
SDIR="${3:-$HOME/logs}"
export CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-report}"
runs=(results/batch10k-b*-"$STAMP")
for r in "${runs[@]}"; do
  rep="$r/rr/rep1"
  [ -f "$rep/per_doc.jsonl" ] || { echo "skip $r (no records)"; continue; }
  echo "=== $(basename "$r")"
  extras=$(python3 - "$rep" "$SDIR" <<'PY'
import json, sys, glob, os
rep, sdir = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(os.path.join(rep, "per_doc.jsonl")) if l.strip()]
meta = next(r for r in rows if r.get("kind") == "shot_meta")
recs = [r for r in rows if not r.get("kind")]
off = meta["mono_offset_ns"]
t0 = (min(r["submit_ns"] for r in recs) + off) / 1e9
t1 = (max(r["completion_ns"] for r in recs) + off) / 1e9
picked = []
for f in sorted(glob.glob(os.path.join(sdir, "sampler_full_*.jsonl"))):
    lines = [l for l in open(f) if l.strip()]
    if len(lines) < 2: continue
    try:
        a, b = json.loads(lines[0])["ts"], json.loads(lines[-1])["ts"]
    except Exception:
        continue
    if a <= t1 + 30 and b >= t0:          # any sample inside the window (+30 s tail)
        picked.append(f)
print(" ".join(picked))
PY
)
  if [ -n "$extras" ]; then
    echo "  merging: $extras"
    python3 bench/merge_samplers.py "$rep" $extras | grep -E 'n_merged|runner|sampler_full' || echo "  !!! merge failed"
  else
    echo "  no supplementary sampler stream overlaps this cell's window"
  fi
  python3 bench/report.py "$r" > "$r/report.txt" 2>&1
  grep -E '^RUN (PASS|FAIL)|reason:' "$r/report.txt" | head -4
  python3 - "$r" <<'PY'
import json, sys
j = json.load(open(sys.argv[1] + "/RUN_REPORT.json")); a = j["arms"]["rocketride"]; rep = a["reps"][0]
m = rep.get("m7_resources", {}); e = rep.get("m7_efficiency", {}); t = rep.get("m1_throughput", {}); l = rep.get("m2_latency", {}); c = rep.get("concurrency", {})
print(f"  docs {rep['census'].get('completed')}/{rep['census'].get('offered')} chunks {t.get('successful_chunks')} span {t.get('window_span_s')} s  docs/s {t.get('docs_per_s')} chunks/s {t.get('chunks_per_s')}")
print(f"  cores {m.get('effective_cores')} util {e.get('cpu_utilization')} cpu/doc {e.get('cpu_seconds_per_doc')} cpu/chunk {e.get('cpu_seconds_per_chunk')} rss_peak {(m.get('rss_mb') or {}).get('peak')} thr_peak {(m.get('threads') or {}).get('peak')}")
print(f"  lat p50 {l.get('p50')} p95 {l.get('p95')} p99 {l.get('p99')} max {l.get('max')} ttfr {rep.get('ttfr_s')} conc peak {c.get('achieved_peak')} mean {c.get('achieved_mean_time_weighted')}")
cov = m.get("coverage") or {}
print(f"  coverage: end_anchor={cov.get('end_anchor')} uncovered_tail={cov.get('uncovered_tail_s')} max_gap={cov.get('max_sample_gap_s')} {cov.get('WARNING','')}")
print(f"  M0 {'PASS' if a.get('m0_PASS') else 'FAIL'} determinism={a.get('determinism_PASS')} basis={(a.get('determinism') or {}).get('basis') if isinstance(a.get('determinism'), dict) else None} provenance={'ok' if j.get('provenance',{}).get('PASS') else 'INCOMPLETE'}")
PY
  aws s3 cp "$r/" "$S3/$(basename "$r")/" --recursive --only-show-errors && echo "  re-uploaded -> $S3/$(basename "$r")/"
done
