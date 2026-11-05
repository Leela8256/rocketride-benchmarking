#!/usr/bin/env bash
# RocketRide posture sweep on films50 — does a decode-heavy corpus prefer fewer
# tasks with more torch threads?
#
# On AMI (decode = 4% of stage time) 32 tasks x OMP 1 was optimal: 16.213 f/s.
# On films (decode = 16%) that posture fell to 8.933 f/s, because 32 tasks means
# 32 decoders competing with 32 detectors for 32 cores. These two cells give
# decode and inference more room per task.
#
# In-flight width is held at 32 box-wide in EVERY cell (tasks x inflight = 32),
# matching the 32x1 baseline, so the only variable is the tasks/threads split.
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-48}"
SRC="$HOME/bench_corpus_films_v2_500"

# tasks  omp  (threads_per_conn = inflight = omp, so tasks x inflight = 32)
CELLS="${CELLS:-
16 2
8 4
}"

echo "=== RocketRide films posture sweep — N=$N, stamp $STAMP"
echo "$CELLS" | grep -v '^[[:space:]]*$' | while read -r T O; do
  [ -n "${T:-}" ] || continue
  echo
  echo "############ FILMS50 — RR ${T} tasks x OMP ${O}  (in-flight $((T*O)) box-wide)"
  RUN_NAME="rrfilms-${T}x${O}-${STAMP}" \
  ARM_ORDER="rr" N="$N" \
  SRC="$SRC" PIN="corpus/sets/archive_films_50.txt" \
  M8="$HOME/bench_corpus_films_m8_50" CORPUS_EXT=".mp4" \
  RR_TASKS="$T" RR_BLAS_THREADS="$O" \
  RR_THREADS_PER_CONN="$O" RR_INFLIGHT_PER_TASK="$O" \
  bash run/matched8x4_ami.sh < /dev/null || echo "!!! CELL ${T}x${O} FAILED (rc=$?) — continuing"
done

echo
echo "=== RR FILMS POSTURE SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
print("%-10s %9s %8s %8s %11s %9s %8s" % ("cell","frames/s","xRT","cores","cpu-s/fr","peak GB","idle"))
print("%-10s %9s %8s %8s %11s %9s %8s" % ("32x1*","8.933","131.8","23.51","2.632","58.9","8.55"))
for d in sorted(glob.glob("results/rrfilms-*-%s" % st)):
    cell = os.path.basename(d).replace("rrfilms-","").replace("-%s" % st,"")
    g = {}
    rep = os.path.join(d,"report.txt")
    if os.path.exists(rep):
        for line in open(rep):
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m: continue
            try: b = json.loads(m.group(2))
            except Exception: continue
            for k in ("frames_per_s","x_realtime","effective_cores","cpu_s_per_frame",
                      "peak_mem_bytes","idle_burden_cores"):
                if k in b and k not in g: g[k] = b[k]
    mem = g.get("peak_mem_bytes")
    print("%-10s %9s %8s %8s %11s %9s %8s" % (cell, g.get("frames_per_s","-"),
        g.get("x_realtime","-"), g.get("effective_cores","-"), g.get("cpu_s_per_frame","-"),
        ("%.1f" % (mem/1e9)) if mem else "-", g.get("idle_burden_cores","-")))
print("* 32x1 is the baseline already measured (filmsbest-50-20260903T040810Z)")
PY

touch "results/rrfilms-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
