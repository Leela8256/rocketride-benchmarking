#!/usr/bin/env bash
# Wave campaign runner, ON THE BOX — one engine, corpus in S3, waves staged
# through RAM. Implements DATA_FLOW_PLAN.md §3–§5 exactly:
#
#   S3 ──► /dev/shm/wave (RAM) ──► client ──► engine ──► records ──► S3
#                                                          (60 s live sync)
#   teardown after every wave: engine container removed (its upload scratch
#   is only released by removal), RAM slice deleted, wave marked done.
#   Resumable: re-running the same CAMP skips finished waves.
#
# Corpus modes:
#   CORPUS_MODE=s3         (default) waves are consecutive slices of the
#                          manifest-ordered corpus at $S3_CORPUS
#   CORPUS_MODE=replicate  cycle the ami30test set into TOTAL hardlinked
#                          docs inside RAM — scale/stress runs only
#                          (content repeats every 30 videos)
#
#   TOTAL=170 W=85 CORPUS_MODE=s3 S3_CORPUS=s3://.../corpus/ami_full \
#     nohup bash run/run_waves.sh > ~/logs/waves.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

TOTAL="${TOTAL:-60}"
W="${W:-60}"
CORPUS_MODE="${CORPUS_MODE:-s3}"
S3_CORPUS="${S3_CORPUS:-s3://rocketride-benchmark-data/leela/corpus/ami30test}"
CAMP="${CAMP:-waves-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="results/$CAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/$CAMP/"
WAVE_DIR="/dev/shm/wave_corpus"
BASE_DIR="/dev/shm/base_corpus"      # replicate mode: resident source set
export BENCH_PIPE="${BENCH_PIPE:-/pipe/benchmark_video_detect.pipe}"
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT"

echo "== preflight"
# S3 GET is load-bearing (corpus pulls, resume); verify the grant NOW.
if ! "$AWS_BIN" s3 ls "$S3_CORPUS/" >/dev/null 2>&1; then
  echo "FATAL: instance role cannot list $S3_CORPUS — check s3:GetObject/ListBucket" >&2
  exit 1
fi
docker container prune -f >/dev/null
rm -rf "$HOME/ami_cache"
# RAM: one wave (+ base set in replicate mode) must fit in /dev/shm.
SHM_FREE_GB=$(df -Pm /dev/shm | awk 'NR==2{print int($4/1024)}')
NEED_SHM_GB=$(( W * 141 / 1024 + 5 ))
[ "$SHM_FREE_GB" -ge "$NEED_SHM_GB" ] || { echo "FATAL: /dev/shm ${SHM_FREE_GB}G free, wave needs ~${NEED_SHM_GB}G — lower W" >&2; exit 1; }
# EBS: only the engine's scratch lands here (~ wave size), plus headroom.
DISK_FREE_GB=$(df -Pm / | awk 'NR==2{print int($4/1024)}')
NEED_DISK_GB=$(( W * 141 / 1024 + 6 ))
[ "$DISK_FREE_GB" -ge "$NEED_DISK_GB" ] || { echo "FATAL: ${DISK_FREE_GB}G disk free, engine scratch needs ~${NEED_DISK_GB}G per wave" >&2; exit 1; }
echo "   ram ok (${SHM_FREE_GB}G free), disk ok (${DISK_FREE_GB}G free)"

echo "== corpus listing"
# The wave slices are consecutive runs of the SORTED object list — stable
# identity, same discipline as every fetch script in this repo.
"$AWS_BIN" s3 ls "$S3_CORPUS/" | awk '/\.avi$/{print $NF}' | sort > "$OUT/corpus_objects.txt"
N_OBJECTS=$(wc -l < "$OUT/corpus_objects.txt" | tr -d ' ')
"$AWS_BIN" s3 cp "$S3_CORPUS/corpus_manifest.json" "$OUT/corpus_manifest.json" --quiet || true
echo "   $N_OBJECTS videos in $S3_CORPUS"
if [ "$CORPUS_MODE" = "s3" ] && [ "$TOTAL" -gt "$N_OBJECTS" ]; then
  echo "FATAL: TOTAL=$TOTAL but corpus has $N_OBJECTS videos" >&2; exit 1
fi
if [ "$CORPUS_MODE" = "replicate" ] && [ ! -d "$BASE_DIR" ]; then
  echo "   replicate mode: pulling base set to RAM (resident for the campaign)"
  mkdir -p "$BASE_DIR"
  "$AWS_BIN" s3 cp "$S3_CORPUS/" "$BASE_DIR/" --recursive --quiet
fi

echo "== build images"
docker compose build rocketride smoke

STATE="$OUT/waves_done"
touch "$STATE"
N_WAVES=$(( (TOTAL + W - 1) / W ))
echo "== campaign $CAMP: $TOTAL docs, $N_WAVES wave(s) of <=$W, mode=$CORPUS_MODE, pipe=$(basename "$BENCH_PIPE")"

for wave in $(seq 1 "$N_WAVES"); do
  if grep -qx "wave$wave" "$STATE"; then echo "== wave $wave already done, skip"; continue; fi
  n_this=$(( TOTAL - (wave - 1) * W )); [ "$n_this" -gt "$W" ] && n_this=$W
  offset=$(( (wave - 1) * W ))

  echo "== wave $wave/$N_WAVES ($n_this docs) — STAGE: S3 -> RAM"
  rm -rf "$WAVE_DIR"; mkdir -p "$WAVE_DIR"
  t0=$(date +%s)
  if [ "$CORPUS_MODE" = "s3" ]; then
    # Slice [offset, offset+n) of the sorted corpus, pulled straight to RAM.
    sed -n "$((offset + 1)),$((offset + n_this))p" "$OUT/corpus_objects.txt" > "$WAVE_DIR/.slice"
    while read -r obj; do
      "$AWS_BIN" s3 cp "$S3_CORPUS/$obj" "$WAVE_DIR/$obj" --quiet &
      while [ "$(jobs -rp | wc -l)" -ge 8 ]; do wait -n; done
    done < "$WAVE_DIR/.slice"
    wait
    cp "$OUT/corpus_manifest.json" "$WAVE_DIR/corpus_manifest.json" 2>/dev/null || true
  else
    # Hardlink replicas WITHIN RAM: distinct doc identities, zero extra bytes.
    python3 - "$BASE_DIR" "$WAVE_DIR" "$wave" "$n_this" "$W" <<'PY'
import json, os, pathlib, sys
src, dst, wave, n, w = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
vids = sorted(src.glob("*.avi"))
durs = {}
mf = src / "corpus_manifest.json"
if mf.exists():
    durs = json.loads(mf.read_text()).get("duration_s", {})
out = {}
for i in range(n):
    g = (wave - 1) * w + i
    orig = vids[g % len(vids)]
    name = f"w{wave:03d}_{i:04d}_{orig.name}"
    os.link(orig, dst / name)
    out[name] = durs.get(orig.name, 0)
(dst / "corpus_manifest.json").write_text(json.dumps({
    "corpus": "replicated-in-ram", "wave": wave, "duration_s": out,
    "note": "hardlinked replicas — scale/stress corpus, content repeats"}))
PY
  fi
  got=$(find "$WAVE_DIR" -name '*.avi' | wc -l | tr -d ' ')
  echo "   staged $got videos to RAM in $(( $(date +%s) - t0 ))s"
  [ "$got" -eq "$n_this" ] || { echo "FATAL: staged $got, expected $n_this" >&2; exit 1; }

  echo "== wave $wave — BOOT engine"
  docker compose up -d rocketride
  for i in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 60 ] && { echo "FATAL: engine never healthy"; exit 1; }
    sleep 5
  done
  echo "   engine healthy"

  ( echo "ts,cpu_usage_usec,mem_current,pids,anon_bytes"
    while docker inspect -f '{{.State.Running}}' videobench-rocketride 2>/dev/null | grep -q true; do
      line=$(docker exec videobench-rocketride sh -c \
        'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/pids.current 2>/dev/null || echo 0; awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null || echo 0' \
        2>/dev/null | tr '\n' ',') || line=""
      [ -n "$line" ] && echo "$(date +%s),${line%,}"
      sleep 15
    done ) > "$OUT/wave${wave}_cgroup.csv" &
  SAMPLER_PID=$!
  ( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
  SYNC_PID=$!
  trap 'kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true' EXIT

  echo "== wave $wave — PROCESS ($n_this docs, blast)"
  rc=0
  CORPUS="$WAVE_DIR" docker compose run --rm smoke \
    python /bench/bench_video.py /corpus "/results/$CAMP/wave$wave" "$n_this" blast 0 \
    > "$OUT/wave${wave}_driver.log" 2>&1 || rc=$?

  kill $SAMPLER_PID $SYNC_PID 2>/dev/null || true
  echo "== wave $wave — TEARDOWN (driver rc=$rc)"
  docker compose logs --no-color rocketride > "$OUT/wave${wave}_engine.log" 2>&1 || true
  docker compose down                 # releases the engine's upload scratch
  rm -rf "$WAVE_DIR"                  # releases the wave's RAM
  if [ "$rc" = "0" ]; then
    echo "wave$wave" >> "$STATE"
  else
    echo "WAVE $wave FAILED — not marked done; rerun the same CAMP to retry it"
  fi
  "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet || true
  df -h / | tail -1; df -h /dev/shm | tail -1
done

rm -rf "$BASE_DIR" 2>/dev/null || true
done_n=$(wc -l < "$STATE" | tr -d ' ')
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet || true
echo "campaign $CAMP finished: $done_n/$N_WAVES waves complete, results: $S3_DEST"
[ "$done_n" -eq "$N_WAVES" ]
