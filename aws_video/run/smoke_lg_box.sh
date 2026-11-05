#!/usr/bin/env bash
# LangGraph arm smoke, ON THE BOX: build the arm's image, boot the service,
# push 3 corpus videos (pulled from S3 — the EBS corpus was deliberately
# deleted in the S3-architecture test) through it, verify, upload results.
# First box-side execution of the arm; gives the first honest x86 numbers
# (the Mac smoke used Apple-silicon torch and is not comparable).
#
#   nohup bash run/smoke_lg_box.sh > ~/logs/lgsmoke.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="results/lgsmoke-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/lgsmoke-$STAMP/"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/ami30test"
VIDEOS="${VIDEOS:-ES2016d.avi ES2003b.avi IB4001.avi}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT" "$HOME/lg_smoke_videos"

echo "== [1/5] videos from S3"
for v in $VIDEOS; do
  [ -s "$HOME/lg_smoke_videos/$v" ] \
    || "$AWS_BIN" s3 cp "$S3_CORPUS/$v" "$HOME/lg_smoke_videos/" --quiet
done
ls -la "$HOME/lg_smoke_videos/"

echo "== [2/5] build LangGraph image"
docker build -t videobench-langgraph:v1 arms/langgraph

echo "== [3/5] service up (shared model cache; first boot downloads rfdetr+miniLM)"
docker rm -f videobench-lg 2>/dev/null || true
docker run -d --name videobench-lg -p 8200:8200 \
  -v rr-model-cache:/root/.cache videobench-langgraph:v1

echo "== [4/5] smoke"
python3 -m pip install --user --quiet requests 2>/dev/null || true
set +e
files=""
for v in $VIDEOS; do files="$files $HOME/lg_smoke_videos/$v"; done
python3 arms/langgraph/smoke_lg.py http://127.0.0.1:8200 $files 2>&1 | tee "$OUT/smoke.log"
rc=${PIPESTATUS[0]}
set -e

echo "== [5/5] collect + teardown (smoke exit=$rc)"
docker logs videobench-lg > "$OUT/service.log" 2>&1 || true
docker rm -f videobench-lg
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "lgsmoke done, exit=$rc, results: $OUT"
exit "$rc"
