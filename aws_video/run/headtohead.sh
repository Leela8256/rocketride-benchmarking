#!/usr/bin/env bash
# Head-to-head SMOKE, ON THE BOX: both frameworks over the same 30 videos,
# one engine each, ONE ARM AT A TIME, all 32 cores available (UNPINNED —
# this is a framework+metrics shakedown, not the enveloped benchmark),
# mode c6 (6 videos in flight), 28 measured + 2 warm for each arm.
#
# Ends by running bench/report.py --arms on the two result dirs — the
# first full exercise of the V0 gates and V1-V5 metrics across arms.
#
#   nohup bash run/headtohead.sh > ~/logs/h2h.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-28}"
WARM="${WARM:-2}"
MODE="${MODE:-c6}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami30test}"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/ami30test"
OUT="results/h2h-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/h2h-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT/rr" "$OUT/lg"

sampler() {  # $1 container, $2 csv
  ( echo "ts,cpu_usage_usec,mem_current,pids,anon_bytes"
    while docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; do
      line=$(docker exec "$1" sh -c \
        'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/pids.current 2>/dev/null || echo 0; awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null || echo 0' \
        2>/dev/null | tr '\n' ',') || line=""
      [ -n "$line" ] && echo "$(date +%s),${line%,}"
      sleep 15
    done ) > "$2" &
  echo $!
}

echo "== [1/6] corpus: 30 videos from S3 (cache-if-present)"
# mkdir FIRST: find on a nonexistent dir exits 1, and under pipefail+set -e
# that killed the whole script silently right here (bit twice, 2026-08-21).
mkdir -p "$CORPUS_DIR"
have=$(find "$CORPUS_DIR" -name '*.avi' | wc -l | tr -d ' ')
if [ "$have" -lt 30 ]; then
  "$AWS_BIN" s3 cp "$S3_CORPUS/" "$CORPUS_DIR/" --recursive --quiet
fi
echo "   $(find "$CORPUS_DIR" -name '*.avi' | wc -l | tr -d ' ') videos, $(du -sh "$CORPUS_DIR" | cut -f1)"

echo "== [2/6] build images"
docker compose build rocketride langgraph smoke

( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID 2>/dev/null || true' EXIT

echo "== [3/6] ARM 1: RocketRide ($MODE, $N docs + $WARM warm, 32 cores unpinned)"
docker compose up -d rocketride
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: RR engine never healthy"; exit 1; }
  sleep 5
done
RR_SAMPLER=$(sampler videobench-rocketride "$OUT/rr/engine_cgroup.csv")
rc_rr=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/h2h-$STAMP/rr" "$N" "$MODE" "$WARM" \
  > "$OUT/rr/driver.log" 2>&1 || rc_rr=$?
kill "$RR_SAMPLER" 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/rr/service.log" 2>&1 || true
docker compose stop rocketride && docker compose rm -f rocketride
echo "   RR done (rc=$rc_rr)"

echo "== [4/6] ARM 2: LangGraph ($MODE, $N docs + $WARM warm, 32 cores unpinned)"
docker compose up -d langgraph
for i in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 90 ] && { echo "FATAL: LG service never healthy"; exit 1; }
  sleep 5
done
LG_SAMPLER=$(sampler videobench-langgraph "$OUT/lg/engine_cgroup.csv")
rc_lg=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/lg_driver.py /corpus "/results/h2h-$STAMP/lg" "$N" "$MODE" "$WARM" \
  > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
kill "$LG_SAMPLER" 2>/dev/null || true
docker compose logs --no-color langgraph > "$OUT/lg/service.log" 2>&1 || true
docker compose down
echo "   LG done (rc=$rc_lg)"

echo "== [5/6] report (gates first, fail-closed)"
rc_rep=0
python3 bench/report.py --arms "$OUT/rr" "$OUT/lg" > "$OUT/report.txt" 2>&1 || rc_rep=$?
cat "$OUT/report.txt"

echo "== [6/6] final sync"
kill $SYNC_PID 2>/dev/null || true
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "h2h done, rr=$rc_rr lg=$rc_lg report=$rc_rep, results: $OUT"
exit $(( rc_rr + rc_lg ))
