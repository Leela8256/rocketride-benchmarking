#!/usr/bin/env bash
# Best-config benchmark on archive_films_v2, both arms at their AMI-measured optimum:
#   RocketRide  32 tasks x OMP 1              (16.213 f/s on AMI)
#   LangGraph   4 proc x c48 x torch 1        (14.712 f/s on AMI)
#
# Stage 1 is films50 as a guard, stage 2 is films500 and runs ONLY if both arms
# passed stage 1. Films are ~90-min 1080p: ~6x AMI's pixels per frame and ~2.6x the
# chunk mass, and the AMI 32-task cell already peaked at 61.1 GB against a 58 GB
# limit. Admission is therefore pinned to ONE video per task (32 in flight, not 64).
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SRC="$HOME/bench_corpus_films_v2_500"

stage () {
  local SUBSET="$1" N=$(( $1 - 2 ))
  echo
  echo "############ FILMS $SUBSET — RR 32x1 + LG 4proc c48 t1  (N=$N measured, 2 warm)"
  RUN_NAME="filmsbest-${SUBSET}-${STAMP}" \
  ARM_ORDER="rr lg" N="$N" \
  SRC="$SRC" PIN="corpus/sets/archive_films_${SUBSET}.txt" \
  M8="$HOME/bench_corpus_films_m8_${SUBSET}" CORPUS_EXT=".mp4" \
  RR_TASKS=32 RR_BLAS_THREADS=1 RR_THREADS_PER_CONN=1 RR_INFLIGHT_PER_TASK=1 \
  LG_WORKERS=4 LG_PER_ENDPOINT_CONCURRENCY=12 LG_EXPECT_DETECT_CONC=12 \
  LG_TORCH_THREADS=1 LG_THREAD_LIMIT=64 \
  bash run/matched8x4_ami.sh < /dev/null
  return $?
}

passed () {   # both arms PASS in the stage report?
  local d="results/filmsbest-$1-${STAMP}"
  [ -f "$d/report.txt" ] || return 1
  [ "$(grep -c 'validity_status: PASS' "$d/report.txt")" -ge 1 ] || return 1
  grep -q 'validity_status: FAIL' "$d/report.txt" && return 1
  return 0
}

stage 50 || echo "!!! FILMS50 returned nonzero — inspecting before deciding on 500"
if passed 50; then
  echo "=== films50 clean — proceeding to films500 ==="
  stage 500 || echo "!!! FILMS500 returned nonzero"
else
  echo "=== films50 did NOT pass — STOPPING, not spending ~7h on films500 ==="
fi

echo
echo "=== FILMS BEST-CONFIG SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
print("%-24s %-4s %9s %8s %8s %11s %9s" % ("run","arm","frames/s","xRT","cores","cpu-s/fr","peak GB"))
for d in sorted(glob.glob("results/filmsbest-*-%s" % st)):
    for arm in ("rr", "lg"):
        rep = os.path.join(d, "report.txt")
        if not os.path.exists(rep):
            continue
        g, seen = {}, 0
        for line in open(rep):
            if re.match(r"\s*== .*/%s\b" % arm, line):
                seen = 1
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m or not seen:
                continue
            try: b = json.loads(m.group(2))
            except Exception: continue
            for k in ("frames_per_s","x_realtime","effective_cores","cpu_s_per_frame","peak_mem_bytes"):
                if k in b and k not in g: g[k] = b[k]
        mem = g.get("peak_mem_bytes")
        print("%-24s %-4s %9s %8s %8s %11s %9s" % (os.path.basename(d)[:24], arm,
            g.get("frames_per_s","-"), g.get("x_realtime","-"), g.get("effective_cores","-"),
            g.get("cpu_s_per_frame","-"), ("%.1f" % (mem/1e9)) if mem else "-"))
PY

touch "results/filmsbest-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
