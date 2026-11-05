#!/usr/bin/env bash
# Object-detection-only sizing run, ON THE BOX: the same ami30h corpus
# (60 measured + 2 warm), ONE native send_files blast through
# pipe/benchmark_video_detect.pipe (audio removed). RocketRide only — the
# LangGraph arm is deliberately untouched (code and Dockerfiles stay in the
# repo; nothing here builds or runs them).
#
# Differences from run30h.sh, all requested after the ENOSPC run:
#   - cleanup first: stopped containers, dangling layers, raw AMI cache,
#     dead partial-result dirs — reuse the corpus, image and model cache
#   - results STREAM to S3 every 60 s while the run is live (progress.jsonl
#     gains a line per video as its terminal event arrives)
#   - sampler survives transient docker-exec failures (the 08-19 one died
#     silently on the first hiccup)
#
#   nohup bash run/run_detect.sh > ~/logs/rundetect.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-60}"
MODE="${MODE:-blast}"
WARM="${WARM:-2}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami30h}"
OUT="results/rundetect-$STAMP"
S3_DEST="${S3_DEST:-s3://rocketride-benchmark-data/leela/videobench/rundetect-$STAMP/}"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
# aws lives in ~/.local/bin, which nohup/sh PATHs miss (the matched_run.sh
# trap) — resolve it once, up front.
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"

echo "== [1/6] cleanup (reuse corpus/image/models; delete the rest)"
docker container prune -f | tail -1
docker image prune -f | tail -1
rm -rf "$HOME/ami_cache"
# dead partial results from killed runs: anything not yet in S3 is pushed
# tiny, then removed
for d in results/run30h-* results/rundetect-*; do
  [ -d "$d" ] || continue
  "$AWS_BIN" s3 sync "$d" "s3://rocketride-benchmark-data/leela/videobench/$(basename "$d")/" --quiet 2>/dev/null || true
  rm -rf "$d"
done
df -h / | tail -1

echo "== [2/6] corpus: reuse if verified"
MEETING_LIST=corpus/sets/ami30h.txt bash corpus/fetch_ami.sh "$N" "$CORPUS_DIR"
NEED_GB=$(( $(du -sm "$CORPUS_DIR" | cut -f1) / 1024 + 6 ))
FREE_GB=$(df -Pm / | awk 'NR==2{print int($4/1024)}')
if [ "$FREE_GB" -lt "$NEED_GB" ]; then
  echo "FATAL: ${FREE_GB}G free, need ~${NEED_GB}G (engine stores uploads on this volume)" >&2
  exit 1
fi
echo "   disk ok: ${FREE_GB}G free, ~${NEED_GB}G needed"

echo "== [3/6] build + engine up"
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

echo "== [4/6] sampler + live S3 sync (60s) on"
(
  echo "ts,cpu_usage_usec,mem_current,pids,anon_bytes"
  while docker inspect -f '{{.State.Running}}' videobench-rocketride 2>/dev/null | grep -q true; do
    line=$(docker exec videobench-rocketride sh -c \
      'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/pids.current 2>/dev/null || echo 0; awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null || echo 0' \
      2>/dev/null | tr '\n' ',') || line=""
    [ -n "$line" ] && echo "$(date +%s),${line%,}"
    sleep 15
  done
) > "$OUT/engine_cgroup.csv" &
SAMPLER_PID=$!
(
  while true; do
    "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true
    sleep 60
  done
) &
SYNC_PID=$!
trap 'kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true' EXIT

echo "== [5/6] run: $N docs, pipe=$(basename "$BENCH_PIPE"), mode=$MODE, warm=$WARM"
rc=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/rundetect-$STAMP" "$N" "$MODE" "$WARM" \
  || rc=$?

echo "== [6/6] collect + teardown (driver exit=$rc)"
kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/engine.log" 2>&1 || true
df -h / | tail -1 > "$OUT/disk_after.txt"
docker compose down
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" && echo "uploaded: $S3_DEST" \
  || echo "WARN: final s3 sync failed (results remain in $OUT)"
echo "rundetect done, exit=$rc, results: $OUT"
exit "$rc"
