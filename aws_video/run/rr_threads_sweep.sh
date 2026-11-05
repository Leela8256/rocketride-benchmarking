#!/usr/bin/env bash
# RocketRide THREAD-SCALING SWEEP, ON THE BOX: the SAME ~30-min AMI docs,
# blast mode, a FRESH ENGINE PER CONFIG; RR_THREADS in {unset,8,32,64,128}.
#
# Question: does use(threads=N) move the ~6-core utilization ceiling?
# The 2026-08-20 probe was a single point (threads=32, no-op); this is the
# systematic sweep requested before committing ~24 h to films500. Same
# instrumentation as the repaired pipeline: measurement markers, windowed
# CPU, per-config gates + report, everything synced to S3.
#
#   nohup bash run/rr_threads_sweep.sh > ~/logs/rrsweep.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-16}"
WARM="${WARM:-1}"
SWEEP="${SWEEP:-unset 8 32 64 128}"
SRC="${SRC:-$HOME/bench_corpus_ami_full}"
CORPUS_DIR="$HOME/bench_corpus_ami30_sweep"
OUT="results/rrsweep-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/rrsweep-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-14400}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT" "$CORPUS_DIR"

# Keepalive: config gaps (engine restarts) are low-CPU — idle-watchdog bait.
( while :; do :; done ) &
KEEPALIVE_PID=$!
trap 'kill $KEEPALIVE_PID 2>/dev/null || true' EXIT

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

echo "== corpus: $((N + WARM)) AMI docs, 25-40 min band, hardlinked from $SRC"
[ -f "$SRC/corpus_manifest.json" ] || { echo "FATAL: no AMI manifest in $SRC" >&2; exit 1; }
python3 - "$SRC/corpus_manifest.json" "$SRC" "$CORPUS_DIR" "$((N + WARM))" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1]))
durs = m["duration_s"]
picks = sorted(d for d, s in durs.items() if 1500 <= s <= 2400)[: int(sys.argv[4])]
assert len(picks) == int(sys.argv[4]), f"only {len(picks)} docs in the 25-40 min band"
for d in picks:
    src, dst = os.path.join(sys.argv[2], d), os.path.join(sys.argv[3], d)
    assert os.path.exists(src), f"missing on disk: {d}"
    if not os.path.exists(dst):
        os.link(src, dst)   # hardlink: zero copy, same filesystem
print(f"   {len(picks)} docs, {sum(durs[d] for d in picks)/3600:.2f} h footage")
PY
cp "$SRC/corpus_manifest.json" "$CORPUS_DIR/corpus_manifest.json"

docker compose build rocketride smoke

for T in $SWEEP; do
  sub="$OUT/threads_$T"
  mkdir -p "$sub"
  RT=""
  [ "$T" != "unset" ] && RT="$T"
  echo "== config threads=$T"
  docker compose up -d rocketride
  for i in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 60 ] && { echo "FATAL: engine never healthy (threads=$T)"; exit 1; }
    sleep 5
  done
  SMP=$(sampler videobench-rocketride "$sub/engine_cgroup.csv")
  rc=0
  CORPUS="$CORPUS_DIR" RR_THREADS="$RT" docker compose run --rm smoke \
    python /bench/bench_video.py /corpus "/results/rrsweep-$STAMP/threads_$T" "$N" blast "$WARM" \
    > "$sub/driver.log" 2>&1 || rc=$?
  kill "$SMP" 2>/dev/null || true
  docker compose logs --no-color rocketride > "$sub/service.log" 2>&1 || true
  docker compose stop rocketride && docker compose rm -f rocketride
  python3 bench/report.py "$sub" > "$sub/report.txt" 2>&1 || true
  echo "   threads=$T rc=$rc"
done

echo "== sweep summary (same $N docs, blast, fresh engine per config)"
python3 - "$OUT" <<'PY' | tee "$OUT/sweep_summary.txt"
import json, re, sys
from pathlib import Path
rows = []
for sub in sorted(Path(sys.argv[1]).glob("threads_*")):
    rep = (sub / "report.txt").read_text() if (sub / "report.txt").exists() else ""
    v3m = re.search(r"V3 efficiency: (\{.*\})", rep)
    v1m = re.search(r"V1 throughput: (\{.*\})", rep)
    v3 = json.loads(v3m.group(1)) if v3m else {}
    v1 = json.loads(v1m.group(1)) if v1m else {}
    rows.append((sub.name.replace("threads_", ""), v3.get("effective_cores"),
                 v3.get("threads_activated"), v1.get("x_realtime"),
                 v3.get("cpu_s_per_footage_min")))
order = {"unset": -1}
rows.sort(key=lambda r: order.get(r[0], 0) if r[0] == "unset" else int(r[0]))
print(f"{'threads':>8} {'eff_cores':>9} {'threads_act':>11} {'x_realtime':>10} {'cpu_s/fmin':>10}")
for r in rows:
    print(f"{str(r[0]):>8} {str(r[1]):>9} {str(r[2]):>11} {str(r[3]):>10} {str(r[4]):>10}")
PY

"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet && echo "uploaded: $S3_DEST"
echo "RRSWEEP DONE"
