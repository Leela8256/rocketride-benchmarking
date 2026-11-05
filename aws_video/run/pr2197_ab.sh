#!/usr/bin/env bash
# A/B of rocketride-server PR #2197 (length-sorted embedding batching) on the
# video pipeline, AMI corpus, DEFAULT RocketRide posture.
#
#   cell A  stock 3.3.1          RR_LENSORT_PATCH=0   arrival-order batching
#   cell B  3.3.1 + PR #2197     RR_LENSORT_PATCH=1   length-sorted batching
#
# Default posture on both: ONE pipeline, no `threads` argument, the six BLAS/OMP
# vars UNSET (the plain `rocketride` service, not `rocketride-matched`), blast
# ingestion. RocketRide only — running LangGraph twice measures nothing.
#
# Expectation, so the result can be read honestly: the change pays off with
# sentences per encode() call (-1% at 16, +30% at 64, +70% at 256) and the node
# hands encode() one document's chunks at a time. AMI averages 85.4 chunks per
# meeting, so the EMBED STAGE should gain ~30-40% — but embedding is only ~5% of
# this pipeline's work (detection is ~90%), so expect ~1-2% END TO END. That is
# small, and it is measurable only because RocketRide's default posture is the
# most repeatable cell in the study (0.12% pass-to-pass on Ansh's two passes).
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-168}"

cell () {  # $1 label, $2 RR_LENSORT_PATCH
  echo
  echo "############ PR2197 CELL $1  (RR_LENSORT_PATCH=$2, default posture, N=$N)"
  # Rebuild the engine image for this cell. The patch layer is ARG-gated, so
  # flipping it invalidates only that layer.
  RR_LENSORT_PATCH="$2" docker compose build rocketride 2>&1 | tail -3
  echo "   image label:"
  docker image inspect videobench-rocketride:3.3.1 \
    --format '     lensort_patch_pr2197={{index .Config.Labels "benchmark.rocketride.lensort_patch_pr2197"}}' 2>/dev/null || true
  # Prove the bytes in the image match the intent — a silently-skipped patch
  # would otherwise look like "no improvement".
  echo "   in-image verification:"
  docker run --rm --entrypoint sh videobench-rocketride:3.3.1 -c \
    'F=$(find /opt/rocketride -path "*/ai/common/models/transformers/sentence_transformers.py" | head -1); \
     if grep -q "order = sorted(range(len(sentences))" "$F"; then echo "     LENGTH-SORTED batching present"; \
     else echo "     arrival-order batching (stock)"; fi' 2>&1 | tail -2

  RUN_NAME="pr2197-$1-$STAMP" NATIVE_ARMS="rr" N="$N" RR_LENSORT_PATCH="$2" \
    bash run/native170.sh < /dev/null || echo "!!! CELL $1 FAILED (rc=$?) — continuing"
}

cell stock   0
cell patched 1

echo
echo "=== PR2197 A/B SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
rows = {}
print("%-9s %9s %8s %8s %11s %11s %9s" % ("cell","frames/s","xRT","cores","cpu-s/fr","chunks/s","span s"))
for d in sorted(glob.glob("results/pr2197-*-%s" % st)):
    cell = os.path.basename(d).split("-")[1]
    g = {}
    rep = os.path.join(d, "report.txt")
    if os.path.exists(rep):
        for line in open(rep):
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m: continue
            try: b = json.loads(m.group(2))
            except Exception: continue
            for k in ("frames_per_s","x_realtime","effective_cores","cpu_s_per_frame",
                      "chunks_per_s","span_s","cpu_s_per_chunk"):
                if k in b and k not in g: g[k] = b[k]
    rows[cell] = g
    print("%-9s %9s %8s %8s %11s %11s %9s" % (cell, g.get("frames_per_s","-"),
        g.get("x_realtime","-"), g.get("effective_cores","-"), g.get("cpu_s_per_frame","-"),
        g.get("chunks_per_s","-"), g.get("span_s","-")))
a, b = rows.get("stock", {}), rows.get("patched", {})
if a.get("frames_per_s") and b.get("frames_per_s"):
    d = (b["frames_per_s"] / a["frames_per_s"] - 1) * 100
    print("\nend-to-end throughput: %+.2f%% (patched vs stock)" % d)
    if a.get("cpu_s_per_chunk") and b.get("cpu_s_per_chunk"):
        dc = (b["cpu_s_per_chunk"] / a["cpu_s_per_chunk"] - 1) * 100
        print("CPU per chunk:         %+.2f%%  <- the stage the patch touches" % dc)
    print("\nSingle rep each: RocketRide default is ~0.12%% pass-to-pass, so a ~1-2%%")
    print("effect is real, but anything under ~0.5%% is not separable from noise.")
PY

touch "results/pr2197-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
