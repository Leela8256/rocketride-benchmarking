#!/usr/bin/env bash
# films100 head-to-head, both arms at their best. ONE run per config, no repeats.
#
#   cell 1  RR 16 tasks x OMP 2      <- the films optimum (beat 32x1 by 16.5% on films50)
#   cell 2  LG 4 proc x c48 x torch1 <- LG's AMI-tuned best, paired against cell 1
#   cell 3  LG 8 proc x c48 x torch1 <- more GIL split: films have 364 frames/doc vs AMI's 137
#   cell 4  LG 4 proc x c48 x torch2 <- wider lanes, the mirror of RR's 16x2 win
#
# films100 not films50: 48 films against c48 is ONE wave, so LangGraph's span is set by
# its single longest print and it converted only 21.5 of 32 cores. 98 films gives LG ~2
# waves and RR 16x2 ~6.
#
# Every cell is its own invocation preceded by a page-cache PREWARM of the corpus.
# Shashi's films50 campaign showed 18% between byte-identical reps purely from cache
# state (11% iowait, gp3 at its 125 MB/s ceiling), so cell order is otherwise a hidden
# variable. Staged files are hardlinks to the source, so warming the source warms both.
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-98}"
SRC="$HOME/bench_corpus_films_v2_500"
PIN="corpus/sets/archive_films_100.txt"
M8="$HOME/bench_corpus_films_m8_100"

prewarm () {
  local before after
  before=$(awk '/^Cached:/{print int($2/1048576)}' /proc/meminfo)
  awk '!/^#/ && NF {print $1".mp4"}' "$PIN" | head -n "$((N+2))" \
    | sed "s#^#$SRC/#" | xargs -P 4 -n 1 -r cat > /dev/null 2>&1
  after=$(awk '/^Cached:/{print int($2/1048576)}' /proc/meminfo)
  echo "   prewarm: page cache ${before} GB -> ${after} GB"
}

cell () {  # name  arm  env-assignments...
  local name="$1" arm="$2"; shift 2
  echo
  echo "############ FILMS100 CELL $name  (arm=$arm, N=$N)"
  prewarm
  env "$@" \
    RUN_NAME="films100-${name}-${STAMP}" ARM_ORDER="$arm" N="$N" \
    SRC="$SRC" PIN="$PIN" M8="$M8" CORPUS_EXT=".mp4" \
    bash run/matched8x4_ami.sh < /dev/null || echo "!!! CELL $name FAILED (rc=$?) — continuing"
}

cell rr16x2 rr \
  RR_TASKS=16 RR_BLAS_THREADS=2 RR_THREADS_PER_CONN=2 RR_INFLIGHT_PER_TASK=2

RRDIR="results/films100-rr16x2-${STAMP}/rr"

cell lg4x48t1 lg \
  LG_WORKERS=4 LG_PER_ENDPOINT_CONCURRENCY=12 LG_EXPECT_DETECT_CONC=12 \
  LG_TORCH_THREADS=1 LG_THREAD_LIMIT=64 PAIR_RR="$RRDIR"

cell lg8x48t1 lg \
  LG_WORKERS=8 LG_PER_ENDPOINT_CONCURRENCY=6 LG_EXPECT_DETECT_CONC=6 \
  LG_TORCH_THREADS=1 LG_THREAD_LIMIT=64

cell lg4x48t2 lg \
  LG_WORKERS=4 LG_PER_ENDPOINT_CONCURRENCY=12 LG_EXPECT_DETECT_CONC=12 \
  LG_TORCH_THREADS=2 LG_THREAD_LIMIT=64

echo
echo "=== FILMS100 SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
print("%-12s %-4s %9s %8s %8s %11s %9s %7s" % ("cell","arm","frames/s","xRT","cores","cpu-s/fr","peak GB","idle"))
for d in sorted(glob.glob("results/films100-*-%s" % st)):
    cell = os.path.basename(d).replace("films100-","").replace("-%s" % st,"")
    for arm in ("rr","lg"):
        sub = os.path.join(d, arm)
        if not os.path.isdir(sub) or not os.path.exists(os.path.join(d,"report.txt")):
            continue
        g, seen = {}, False
        for line in open(os.path.join(d,"report.txt")):
            if re.match(r"\s*== .*/%s\b" % arm, line): seen = True
            elif re.match(r"\s*== .*/(rr|lg)\b", line): seen = False
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m or not seen: continue
            try: b = json.loads(m.group(2))
            except Exception: continue
            for k in ("frames_per_s","x_realtime","effective_cores","cpu_s_per_frame",
                      "peak_mem_bytes","idle_burden_cores"):
                if k in b and k not in g: g[k] = b[k]
        if not g: continue
        mem = g.get("peak_mem_bytes")
        print("%-12s %-4s %9s %8s %8s %11s %9s %7s" % (cell, arm, g.get("frames_per_s","-"),
            g.get("x_realtime","-"), g.get("effective_cores","-"), g.get("cpu_s_per_frame","-"),
            ("%.1f" % (mem/1e9)) if mem else "-", g.get("idle_burden_cores","-")))
PY

touch "results/films100-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
