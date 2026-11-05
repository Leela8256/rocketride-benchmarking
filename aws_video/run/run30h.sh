#!/usr/bin/env bash
# 30-hour RocketRide sizing run, ON THE BOX: the ami30h set (60 measured
# meetings closest to 30 min + 2 warm), one native send_files blast through
# the video pipe. Purpose: wall time, x-realtime, effective cores, peak RSS,
# disk — the numbers that decide whether video needs more AWS than we have.
#
#   nohup bash run/run30h.sh > ~/logs/run30h.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-60}"
MODE="${MODE:-blast}"
WARM="${WARM:-2}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami30h}"
OUT="results/run30h-$STAMP"
S3_DEST="${S3_DEST:-s3://rocketride-benchmark-data/leela/videobench/run30h-$STAMP/}"
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"   # 6 h for the whole batch

echo "== [1/5] corpus: ami30h set -> $CORPUS_DIR"
MEETING_LIST=corpus/sets/ami30h.txt bash corpus/fetch_ami.sh "$N" "$CORPUS_DIR"
echo "   $(find "$CORPUS_DIR" -name '*.avi' | wc -l | tr -d ' ') videos, $(du -sh "$CORPUS_DIR" | cut -f1)"

# The raw download cache is dead weight once the corpus is muxed and verified,
# and the 2026-08-19 run died on ENOSPC because of it: the ENGINE stores its
# own copy of every uploaded video (+ decode temp) on this same volume, which
# needs roughly another corpus-worth of space. Cache is re-downloadable;
# failed runs are not. Keep it only with KEEP_CACHE=1.
if [ "${KEEP_CACHE:-0}" != "1" ] && [ -d "$HOME/ami_cache" ]; then
  echo "   freeing raw cache: $(du -sh "$HOME/ami_cache" | cut -f1) (KEEP_CACHE=1 to keep)"
  rm -rf "$HOME/ami_cache"
fi

# Fail BEFORE the engine does: corpus-size + temp + results must fit.
NEED_GB=$(( $(du -sm "$CORPUS_DIR" | cut -f1) / 1024 + 6 ))
FREE_GB=$(df -Pm / | awk 'NR==2{print int($4/1024)}')
if [ "$FREE_GB" -lt "$NEED_GB" ]; then
  echo "FATAL: ${FREE_GB}G free, need ~${NEED_GB}G (engine stores uploads on this volume)" >&2
  exit 1
fi
echo "   disk ok: ${FREE_GB}G free, ~${NEED_GB}G needed"

echo "== [2/5] build + engine up"
docker compose build
docker compose up -d rocketride
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null || echo none)"
  [ "$st" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: engine never became healthy"; docker compose logs rocketride | tail -50; exit 1; }
  sleep 5
done
echo "   engine healthy"
mkdir -p "$OUT"

echo "== [3/5] cgroup sampler on (15s cadence)"
(
  # ts,cpu_usage_usec,mem_current_bytes,mem_peak_bytes — cumulative CPU makes
  # effective cores derivable over ANY window: d(usage_usec)/d(t)/1e6.
  echo "ts,cpu_usage_usec,mem_current,mem_peak"
  while docker inspect -f '{{.State.Running}}' videobench-rocketride 2>/dev/null | grep -q true; do
    line=$(docker exec videobench-rocketride sh -c \
      'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0' \
      2>/dev/null | tr '\n' ',') || break
    echo "$(date +%s),${line%,}"
    sleep 15
  done
) > "$OUT/engine_cgroup.csv" &
SAMPLER_PID=$!
trap 'kill $SAMPLER_PID 2>/dev/null || true' EXIT

echo "== [4/5] run: $N docs, mode=$MODE, warm=$WARM, timeout=${BENCH_TIMEOUT_S}s"
rc=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/run30h-$STAMP" "$N" "$MODE" "$WARM" \
  || rc=$?

echo "== [5/5] collect + teardown (driver exit=$rc)"
kill $SAMPLER_PID 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/engine.log" 2>&1 || true
df -h / | tail -1 > "$OUT/disk_after.txt"
docker compose down

if command -v aws >/dev/null 2>&1; then
  aws s3 cp --recursive "$OUT" "$S3_DEST" && echo "uploaded: $S3_DEST" \
    || echo "WARN: s3 upload failed (results remain in $OUT)"
else
  echo "WARN: no aws cli on PATH; results in $OUT"
fi
echo "run30h done, exit=$rc, results: $OUT"
exit "$rc"
