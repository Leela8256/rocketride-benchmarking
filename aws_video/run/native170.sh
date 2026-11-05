#!/usr/bin/env bash
# FULL-CORPUS native-saturation head-to-head, ON THE BOX: all 170 usable
# AMI meetings (99.5 h of footage) through both arms, each on its native
# ingestion path. Same shape as native60.sh, full-corpus scale, corpus
# pulled from the staged S3 set (stage_corpus.sh must have completed).
#
#   RocketRide: blast (one send_files batch)   ~2.7 h at its measured 36x
#   LangGraph:  c170 (whole backlog at t=0)    ~40 min at its measured 150x
#
# 168 measured + 2 warm per arm, 32 cores UNPINNED, single rep — the
# full-corpus sizing run. The matched enveloped campaign is separate.
#
#   nohup bash run/native170.sh > ~/logs/native170.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-168}"
WARM_N="${WARM:-2}"
# WARM is overloaded: this runner uses it as a COUNT of warm videos, but
# compose maps ${WARM} to the smoke container's /warm BIND MOUNT (the matched
# posture's disjoint warm fixtures, docker-compose.yml). A caller that exports
# WARM=2 would therefore make compose bind a directory literally named "2".
# Read it into WARM_N and drop it from the environment: with WARM unset the
# mount falls back to its ./corpus default, which is what every run so far got.
unset WARM
RR_MODE="${RR_MODE:-blast}"
LG_MODE="${LG_MODE:-c170}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami_full}"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/ami_full"
EXPECT=170
RUN_NAME="${RUN_NAME:-native170-$STAMP}"
OUT="results/$RUN_NAME"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/$RUN_NAME/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
# Which arms to run: "rr lg" (default), "rr", or "lg". An A/B of the engine
# image only needs the RocketRide arm; running LangGraph twice measures nothing.
NATIVE_ARMS="${NATIVE_ARMS:-rr lg}"
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

echo "== [1/6] corpus: ami_full from S3"
s3_have=$("$AWS_BIN" s3 ls "$S3_CORPUS/" | grep -c '\.avi$' || true)
[ "$s3_have" -ge "$EXPECT" ] || { echo "FATAL: only $s3_have/$EXPECT staged — run stage_corpus.sh first" >&2; exit 1; }
have=$(find "$CORPUS_DIR" -name '*.avi' | wc -l | tr -d ' ')
if [ "$have" -lt "$EXPECT" ]; then
  "$AWS_BIN" s3 cp "$S3_CORPUS/" "$CORPUS_DIR/" --recursive --quiet
fi
echo "   $(find "$CORPUS_DIR" -name '*.avi' | wc -l | tr -d ' ') videos, $(du -sh "$CORPUS_DIR" | cut -f1)"

echo "== [2/6] build images"
BUILD_TARGETS="smoke"
case " $NATIVE_ARMS " in *" rr "*) BUILD_TARGETS="rocketride $BUILD_TARGETS" ;; esac
case " $NATIVE_ARMS " in *" lg "*) BUILD_TARGETS="langgraph $BUILD_TARGETS" ;; esac
echo "   build targets: $BUILD_TARGETS (NATIVE_ARMS=$NATIVE_ARMS, RR_LENSORT_PATCH=${RR_LENSORT_PATCH:-0})"
docker compose build $BUILD_TARGETS

( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID 2>/dev/null || true' EXIT

rc_rr=0
if case " $NATIVE_ARMS " in *" rr "*) true;; *) false;; esac; then
echo "== [3/6] ARM 1: RocketRide ($RR_MODE, $N docs + $WARM_N warm, unpinned)"
docker compose up -d rocketride
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: RR engine never healthy"; exit 1; }
  sleep 5
done
RR_SAMPLER=$(sampler videobench-rocketride "$OUT/rr/engine_cgroup.csv")
rc_rr=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/$RUN_NAME/rr" "$N" "$RR_MODE" "$WARM_N" \
  > "$OUT/rr/driver.log" 2>&1 || rc_rr=$?
kill "$RR_SAMPLER" 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/rr/service.log" 2>&1 || true
docker compose stop rocketride && docker compose rm -f rocketride
echo "   RR done (rc=$rc_rr)"
fi

rc_lg=0
if case " $NATIVE_ARMS " in *" lg "*) true;; *) false;; esac; then
echo "== [4/6] ARM 2: LangGraph ($LG_MODE, $N docs + $WARM_N warm, unpinned)"
docker compose up -d langgraph
for i in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 90 ] && { echo "FATAL: LG service never healthy"; exit 1; }
  sleep 5
done
LG_SAMPLER=$(sampler videobench-langgraph "$OUT/lg/engine_cgroup.csv")
rc_lg=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/lg_driver.py /corpus "/results/$RUN_NAME/lg" "$N" "$LG_MODE" "$WARM_N" \
  > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
kill "$LG_SAMPLER" 2>/dev/null || true
docker compose logs --no-color langgraph > "$OUT/lg/service.log" 2>&1 || true
docker compose down
fi
echo "   LG done (rc=$rc_lg)"

echo "== [5/6] report"
rc_rep=0
if [ -f "$OUT/rr/per_doc.jsonl" ] && [ -f "$OUT/lg/per_doc.jsonl" ]; then
  python3 bench/report.py --arms "$OUT/rr" "$OUT/lg" > "$OUT/report.txt" 2>&1 || rc_rep=$?
elif [ -f "$OUT/rr/per_doc.jsonl" ]; then
  python3 bench/report.py "$OUT/rr" > "$OUT/report.txt" 2>&1 || rc_rep=$?
elif [ -f "$OUT/lg/per_doc.jsonl" ]; then
  python3 bench/report.py "$OUT/lg" > "$OUT/report.txt" 2>&1 || rc_rep=$?
else echo "no arm produced records" > "$OUT/report.txt"; rc_rep=1; fi
cat "$OUT/report.txt"

echo "== [6/6] final sync"
kill $SYNC_PID 2>/dev/null || true
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "native170 done, rr=$rc_rr lg=$rc_lg report=$rc_rep, results: $OUT"
exit $(( rc_rr + rc_lg ))
