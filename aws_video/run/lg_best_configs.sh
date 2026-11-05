#!/usr/bin/env bash
# LangGraph best-config runs on the FULL AMI corpus — the two winners from the
# 96-video sweep (lgsweep 20260831T221059Z):
#   4 processes x c48, torch 1  -> best throughput      (14.02 f/s)
#   8 processes x c32, torch 1  -> best CPU efficiency  (1.744 CPU-s/frame)
# Deterministic index%W routing via serve8.py — no kernel accept-skew.
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-168}"

CELLS="${CELLS:-
lg_4proc_c48_t1 4 12 1
lg_8proc_c32_t1 8 4 1
}"

echo "=== LangGraph best-config campaign — N=$N, stamp $STAMP"
echo "$CELLS" | grep -v '^[[:space:]]*$' | while read -r name W PER T; do
  [ -n "${name:-}" ] || continue
  echo
  echo "############ LG CELL $name : W=$W per-endpoint=$PER c=$((W*PER)) torch=$T"
  RUN_NAME="lgbest-${name}-${STAMP}" \
  ARM_ORDER=lg N="$N" \
  LG_WORKERS="$W" LG_PER_ENDPOINT_CONCURRENCY="$PER" \
  LG_EXPECT_DETECT_CONC="$PER" LG_TORCH_THREADS="$T" \
  LG_THREAD_LIMIT=64 \
  bash run/matched8x4_ami.sh < /dev/null || echo "!!! CELL $name FAILED (rc=$?) — continuing"
done

echo
echo "=== LG BEST-CONFIG SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
print("%-18s %9s %8s %8s %11s %9s" % ("cell","frames/s","xRT","cores","cpu-s/fr","peak GB"))
for d in sorted(glob.glob("results/lgbest-*-%s" % st)):
    cell = os.path.basename(d).replace("lgbest-","").replace("-%s" % st,"")
    g = {}
    rep = os.path.join(d,"report.txt")
    if os.path.exists(rep):
        for line in open(rep):
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m: continue
            try: b = json.loads(m.group(2))
            except Exception: continue
            for k in ("frames_per_s","x_realtime","effective_cores","cpu_s_per_frame","peak_mem_bytes"):
                if k in b and k not in g: g[k] = b[k]
    mem = g.get("peak_mem_bytes")
    print("%-18s %9s %8s %8s %11s %9s" % (cell, g.get("frames_per_s","-"),
        g.get("x_realtime","-"), g.get("effective_cores","-"),
        g.get("cpu_s_per_frame","-"), ("%.1f" % (mem/1e9)) if mem else "-"))
PY

touch "results/lgbest-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
