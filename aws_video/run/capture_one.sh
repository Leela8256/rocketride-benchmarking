#!/usr/bin/env bash
# Verification capture, ON THE BOX: one corpus video through the detect pipe
# with the FULL response saved, plus an independent ffmpeg frame extraction
# at the same 15 s cadence — so frames, detections, and embeddings can be
# cross-checked offline. Everything lands in S3.
#
#   bash run/capture_one.sh [video_basename]
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami30h}"
VIDEO="${1:-ES2016d.avi}"
OUT="results/capture-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/capture-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
FFMPEG="$(command -v ffmpeg || echo "$HOME/bin/ffmpeg")"

[ -f "$CORPUS_DIR/$VIDEO" ] || { echo "FATAL: $CORPUS_DIR/$VIDEO missing" >&2; exit 1; }
mkdir -p "$OUT/frames"

echo "== engine up"
docker compose up -d rocketride
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null || echo none)"
  [ "$st" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: engine unhealthy"; exit 1; }
  sleep 5
done

echo "== capture: $VIDEO through $BENCH_PIPE"
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/capture_one.py "/corpus/$VIDEO" "/results/capture-$STAMP"

echo "== independent frame extraction (ffmpeg, 1 frame / 15 s)"
"$FFMPEG" -nostdin -loglevel error -i "$CORPUS_DIR/$VIDEO" \
  -vf "fps=1/15" -q:v 4 "$OUT/frames/f_%04d.jpg"
ls "$OUT/frames" | wc -l | xargs echo "   frames extracted:"

docker compose down
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "capture done: $OUT"
