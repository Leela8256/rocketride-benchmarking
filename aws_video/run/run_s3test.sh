#!/usr/bin/env bash
# S3-corpus architecture test, ON THE BOX: prove the corpus can live in S3
# and stage through RAM, leaving engine scratch as the ONLY EBS consumer.
#
#   [1] upload 30 muxed videos + manifest to S3 (idempotent)
#   [2] DELETE the EBS corpus — from here S3 is the only copy (rebuildable
#       from the AMI mirror via fetch_ami.sh if ever needed)
#   [3] pull S3 -> /dev/shm (RAM tmpfs), timed — this is the per-wave load
#       cost of the future s3-mode waves
#   [4] blast the 30 through the detect pipe from RAM, sampling DISK free
#       alongside CPU so the EBS delta during the run is attributable
#   [5] teardown, clear RAM, everything to S3
#
# NOT a wave run, single engine, no warm docs (cold model load lands in the
# span — this is an infrastructure test, not a timing rep).
#
#   nohup bash run/run_s3test.sh > ~/logs/s3test.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-30}"
SRC="${SRC:-$HOME/bench_corpus_ami30h}"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/ami30test"
SHM="/dev/shm/ami30test"
OUT="results/s3test-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/s3test-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT"

have=$("$AWS_BIN" s3 ls "$S3_CORPUS/" 2>/dev/null | grep -c '\.avi$' || true)
if [ "$have" -ge "$N" ]; then
  echo "== [1/7] corpus already staged in S3 ($have videos)"
else
  echo "== [1/7] staging $N videos to $S3_CORPUS"
  [ -d "$SRC" ] || { echo "FATAL: $SRC missing and S3 not staged" >&2; exit 1; }
  i=0
  for f in $(ls "$SRC"/*.avi | sort | head -"$N"); do
    "$AWS_BIN" s3 cp "$f" "$S3_CORPUS/$(basename "$f")" --quiet
    i=$((i+1))
  done
  "$AWS_BIN" s3 cp "$SRC/corpus_manifest.json" "$S3_CORPUS/corpus_manifest.json" --quiet
  echo "   uploaded $i videos + manifest"
  have=$("$AWS_BIN" s3 ls "$S3_CORPUS/" | grep -c '\.avi$')
  [ "$have" -ge "$N" ] || { echo "FATAL: S3 shows $have videos, expected $N" >&2; exit 1; }
fi

echo "== [2/7] deleting EBS corpus (S3 is now the only copy)"
rm -rf "$SRC"
df -h / | tail -1 | tee "$OUT/disk_after_corpus_delete.txt"

echo "== [3/7] pull S3 -> RAM (/dev/shm), timed"
rm -rf "$SHM"; mkdir -p "$SHM"
t0=$(date +%s)
"$AWS_BIN" s3 cp "$S3_CORPUS/" "$SHM/" --recursive --quiet
t1=$(date +%s)
pulled=$(find "$SHM" -name '*.avi' | wc -l | tr -d ' ')
bytes=$(du -sm "$SHM" | cut -f1)
echo "   pulled $pulled videos (${bytes} MB) in $((t1-t0))s" | tee "$OUT/s3_pull_time.txt"
free -g | head -2

echo "== [4/7] engine up"
docker compose build -q rocketride smoke 2>/dev/null || docker compose build rocketride smoke
docker compose up -d rocketride
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null || echo none)"
  [ "$st" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: engine never healthy"; exit 1; }
  sleep 5
done
echo "   engine healthy"

echo "== [5/7] samplers on (cpu + DISK + ram, 15s) + live S3 sync"
(
  echo "ts,cpu_usage_usec,mem_current,disk_free_mb,shm_used_mb"
  while docker inspect -f '{{.State.Running}}' videobench-rocketride 2>/dev/null | grep -q true; do
    c=$(docker exec videobench-rocketride sh -c \
      'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current' \
      2>/dev/null | tr '\n' ',') || c=""
    dfree=$(df -Pm / | awk 'NR==2{print $4}')
    shmu=$(du -sm "$SHM" 2>/dev/null | cut -f1 || echo 0)
    [ -n "$c" ] && echo "$(date +%s),${c%,}$dfree,$shmu"
    sleep 15
  done
) > "$OUT/engine_cgroup.csv" &
SAMPLER_PID=$!
( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true' EXIT

echo "== [6/7] run: $N docs from RAM, blast, warm=0"
rc=0
CORPUS="$SHM" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/s3test-$STAMP" "$N" blast 0 \
  || rc=$?

echo "== [7/7] collect + teardown (driver exit=$rc)"
kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/engine.log" 2>&1 || true
df -h / | tail -1 > "$OUT/disk_after.txt"
docker compose down
rm -rf "$SHM"
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" && echo "uploaded: $S3_DEST"
echo "s3test done, exit=$rc, results: $OUT"
exit "$rc"
