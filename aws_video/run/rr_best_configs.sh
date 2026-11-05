#!/usr/bin/env bash
# RocketRide best-config runs on the FULL AMI corpus — the two cells Shashi
# measured on his harness (32 tasks x OMP 1, 16 tasks x OMP 2). RocketRide only.
#
# Admission is bounded explicitly (threads/conn = inflight = 2). Our earlier
# 8-task cell left it at the engine default — 8 connections x 64 = 512 slots —
# which admitted the whole corpus at once, put 114 ffmpeg decoders on the box and
# pinned memory at the 58 GB limit. At 32 tasks that would be 2,048 slots. Shashi's
# harness does not record its admission width; ours is explicit and in provenance.
#
#   bash run/rr_best_configs.sh          # both cells, N=168
#   N=32 bash run/rr_best_configs.sh     # quick shape check
set -uo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-168}"
CELLS="${CELLS:-32 1
16 2}"

echo "=== RocketRide best-config campaign — N=$N, stamp $STAMP"
echo "$CELLS" | grep -v '^[[:space:]]*$' | while read -r T O; do
  [ -n "${T:-}" ] || continue
  echo
  echo "############ RR CELL ${T} tasks x OMP ${O}   (N=$N docs, full AMI)"
  RUN_NAME="rrbest-${T}x${O}-ami-${STAMP}" \
  ARM_ORDER=rr N="$N" \
  RR_TASKS="$T" RR_BLAS_THREADS="$O" \
  RR_THREADS_PER_CONN=2 RR_INFLIGHT_PER_TASK=2 \
  bash run/matched8x4_ami.sh < /dev/null || echo "!!! CELL ${T}x${O} FAILED (rc=$?) — continuing"
done

echo
echo "=== RR BEST-CONFIG SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
stamp = sys.argv[1]
hdr = ("cell", "frames/s", "xRT", "cores", "cpu-s/frame", "peak GB", "idle")
print("%-12s %10s %9s %8s %12s %9s %7s" % hdr)
for d in sorted(glob.glob("results/rrbest-*-ami-%s" % stamp)):
    cell = os.path.basename(d).replace("rrbest-", "").replace("-ami-%s" % stamp, "")
    rep = os.path.join(d, "report.txt")
    g = {}
    if os.path.exists(rep):
        for line in open(rep):
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m:
                continue
            try:
                b = json.loads(m.group(2))
            except Exception:
                continue
            for k in ("frames_per_s", "x_realtime", "effective_cores",
                      "cpu_s_per_frame", "peak_mem_bytes", "idle_burden_cores"):
                if k in b and k not in g:
                    g[k] = b[k]
    mem = g.get("peak_mem_bytes")
    print("%-12s %10s %9s %8s %12s %9s %7s" % (
        cell, g.get("frames_per_s", "-"), g.get("x_realtime", "-"),
        g.get("effective_cores", "-"), g.get("cpu_s_per_frame", "-"),
        ("%.1f" % (mem / 1e9)) if mem else "-", g.get("idle_burden_cores", "-")))
PY

touch "results/rrbest-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
