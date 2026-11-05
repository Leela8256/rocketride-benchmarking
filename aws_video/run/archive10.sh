#!/usr/bin/env bash
# ARCHIVE FILMS SMOKE, ON THE BOX: the 10-film archive10 set (feature
# films from archive.org, 63-105 min each, ~13.3 h) through both arms —
# the LONG-VIDEO + .mp4 shakedown. Risks under test (ARCHIVE_FILMS.md §6):
# .mp4 routing to the webhook video lane (AMI only proved .avi), vintage
# codecs inside h.264 derivatives, ~2.6x AMI chunk mass per doc.
#
#   RocketRide: blast, default threads    LangGraph: c32, one uvicorn
# 8 measured + 2 warm per arm, 32 cores UNPINNED, single rep.
#
#   nohup bash run/archive10.sh > ~/logs/archive10.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-8}"
WARM="${WARM:-2}"
RR_MODE="${RR_MODE:-blast}"
LG_MODE="${LG_MODE:-c32}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_archive_films}"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/archive_films"
EXPECT="${EXPECT:-10}"
OUT="results/archive10-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/archive10-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT/rr" "$OUT/lg" "$CORPUS_DIR"

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

echo "== [1/6] corpus: archive_films from S3"
s3_have=$("$AWS_BIN" s3 ls "$S3_CORPUS/" | grep -c '\.mp4$' || true)
[ "$s3_have" -ge "$EXPECT" ] || { echo "FATAL: only $s3_have/$EXPECT staged — run stage_corpus.sh first" >&2; exit 1; }
have=$(find "$CORPUS_DIR" -name '*.mp4' | wc -l | tr -d ' ')
if [ "$have" -lt "$EXPECT" ]; then
  "$AWS_BIN" s3 cp "$S3_CORPUS/" "$CORPUS_DIR/" --recursive --quiet
fi
echo "   $(find "$CORPUS_DIR" -name '*.mp4' | wc -l | tr -d ' ') videos, $(du -sh "$CORPUS_DIR" | cut -f1)"

echo "== [2/6] build images"
docker compose build rocketride langgraph smoke

( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID 2>/dev/null || true' EXIT

echo "== [3/6] ARM 1: RocketRide ($RR_MODE, $N docs + $WARM warm, unpinned)"
docker compose up -d rocketride
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: RR engine never healthy"; exit 1; }
  sleep 5
done
RR_SAMPLER=$(sampler videobench-rocketride "$OUT/rr/engine_cgroup.csv")
rc_rr=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/archive10-$STAMP/rr" "$N" "$RR_MODE" "$WARM" \
  > "$OUT/rr/driver.log" 2>&1 || rc_rr=$?
kill "$RR_SAMPLER" 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/rr/service.log" 2>&1 || true
docker compose stop rocketride && docker compose rm -f rocketride
echo "   RR done (rc=$rc_rr)"

echo "== [4/6] ARM 2: LangGraph ($LG_MODE, $N docs + $WARM warm, unpinned)"
docker compose up -d langgraph
for i in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 90 ] && { echo "FATAL: LG service never healthy"; exit 1; }
  sleep 5
done
LG_SAMPLER=$(sampler videobench-langgraph "$OUT/lg/engine_cgroup.csv")
rc_lg=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/lg_driver.py /corpus "/results/archive10-$STAMP/lg" "$N" "$LG_MODE" "$WARM" \
  > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
kill "$LG_SAMPLER" 2>/dev/null || true
docker compose logs --no-color langgraph > "$OUT/lg/service.log" 2>&1 || true
docker compose down
echo "   LG done (rc=$rc_lg)"

echo "== [5/6] report"
rc_rep=0
python3 bench/report.py --arms "$OUT/rr" "$OUT/lg" > "$OUT/report.txt" 2>&1 || rc_rep=$?
cat "$OUT/report.txt"

echo "== [6/6] final sync"
kill $SYNC_PID 2>/dev/null || true
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "archive10 done, rr=$rc_rr lg=$rc_lg report=$rc_rep, results: $OUT"
exit $(( rc_rr + rc_lg ))
