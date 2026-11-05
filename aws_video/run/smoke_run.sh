#!/usr/bin/env bash
# One-video smoke, ON THE BOX. Fetches the first AMI meeting (ES2002a — the
# deterministic N=1 pick of fetch_ami.sh), builds the engine + client images,
# runs the smoke driver, collects the engine log, uploads to S3.
#
#   cd aws_video && bash run/smoke_run.sh
#   REPS=3 N=1 bash run/smoke_run.sh
#
# Long enough to outlive an SSM session — launch detached:
#   nohup bash run/smoke_run.sh > ~/logs/videosmoke.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-1}"
REPS="${REPS:-2}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami_n${N}_off0}"
OUT="results/smoke-$STAMP"
S3_DEST="${S3_DEST:-s3://rocketride-benchmark-data/leela/videobench/smoke-$STAMP/}"

echo "== [1/5] corpus: $N AMI meeting(s) -> $CORPUS_DIR"
bash corpus/fetch_ami.sh "$N" "$CORPUS_DIR"
VIDEO="$(find "$CORPUS_DIR" -name '*.avi' | sort | head -1)"
[ -n "$VIDEO" ] || { echo "FATAL: no video in $CORPUS_DIR" >&2; exit 1; }
echo "   smoke video: $(basename "$VIDEO") ($(du -h "$VIDEO" | cut -f1))"

echo "== [2/5] build images"
docker compose build

echo "== [3/5] engine up"
docker compose up -d rocketride
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null || echo none)"
  [ "$st" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: engine never became healthy"; docker compose logs rocketride | tail -50; exit 1; }
  sleep 5
done
echo "   engine healthy"

echo "== [4/5] smoke: $REPS rep(s) of $(basename "$VIDEO")"
mkdir -p "$OUT"
rc=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python smoke_video.py "/corpus/$(basename "$VIDEO")" "/results/smoke-$STAMP" "$REPS" \
  || rc=$?

echo "== [5/5] collect + teardown (smoke exit=$rc)"
docker compose logs --no-color rocketride > "$OUT/engine.log" 2>&1 || true
docker compose down
# Model cache volume is kept on purpose: next smoke/bench skips the downloads.

if command -v aws >/dev/null 2>&1; then
  aws s3 cp --recursive "$OUT" "$S3_DEST" && echo "uploaded: $S3_DEST" \
    || echo "WARN: s3 upload failed (results remain in $OUT)"
else
  echo "WARN: no aws cli on PATH (see docs/AWS_BOX_ACCESS.md section 5); results in $OUT"
fi

echo "smoke done, exit=$rc, results: $OUT"
exit "$rc"
