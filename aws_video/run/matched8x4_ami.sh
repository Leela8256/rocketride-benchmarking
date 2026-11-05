#!/usr/bin/env bash
# MATCHED POSTURE RUNNER, ON THE BOX — posture matched8x4_native on AMI.
#
#   "Eight-process/model architecture-matched comparison using native
#    ingestion: RocketRide sharded blast versus LangGraph c32 HTTP
#    saturation."  (not a claim that batch and HTTP latency are equivalent)
#
#   RocketRide rr_matched_8x4 : 1 engine container (58g), 8 tasks/tokens,
#                               threads= OMITTED, ttl=0, BLAS/OMP=4, sharded blast
#   LangGraph  lg_matched_8x4_c32: 1 container (58g), 8 uvicorn procs 8201-8208,
#                               torch 4/1, detect concurrency 1, c32 = 8x4
#
# Corpus: AMI ami_full, first N (168) of the committed sorted-ID order measured,
# the last 2 as DISJOINT warm fixtures (separate /warm mount, every process).
# Separate from films_v2.sh / native170.sh; default-posture results untouched.
#
#   ARM_ORDER="rr lg" (default) | "lg rr"      REP=1 (label only)
#   nohup bash run/matched8x4_ami.sh > ~/logs/m8ami.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REP="${REP:-1}"
ARM_ORDER="${ARM_ORDER:-rr lg}"
N="${N:-168}"
NWARM=2
SRC="${SRC:-$HOME/bench_corpus_ami_full}"
PIN="${PIN:-corpus/sets/ami_full.txt}"
M8="${M8:-$HOME/bench_corpus_ami_m8}"
# Corpus shape: AMI pins are bare ids + .avi; films pins are "id<TAB>file.mp4".
CORPUS_EXT="${CORPUS_EXT:-.avi}"
RR_TASKS="${RR_TASKS:-8}"; RR_BLAS_THREADS="${RR_BLAS_THREADS:-4}"
RUN="${RUN_NAME:-matched${RR_TASKS}x${RR_BLAS_THREADS}-ami-rep$REP-$STAMP}"
OUT="results/$RUN"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/$RUN/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-86400}"
export RR_PIPE_TTL_S=0            # verified in engine source: 0 = no expiry
export ROCKETRIDE_URI="ws://rocketride-matched:5565/task/service"
export LG_HOST=langgraph-matched
export RR_TASKS RR_BLAS_THREADS
export LG_TORCH_THREADS="${LG_TORCH_THREADS:-4}" LG_WORKERS="${LG_WORKERS:-8}" LG_PORT_BASE="${LG_PORT_BASE:-8201}"
export LG_PER_ENDPOINT_CONCURRENCY="${LG_PER_ENDPOINT_CONCURRENCY:-4}" LG_EXPECT_DETECT_CONC="${LG_EXPECT_DETECT_CONC:-1}"
export RR_THREADS_PER_CONN="${RR_THREADS_PER_CONN:-}" RR_INFLIGHT_PER_TASK="${RR_INFLIGHT_PER_TASK:-4}"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT/rr/census" "$OUT/lg" "$OUT/provenance" "$M8/measured" "$M8/warm"

( while :; do :; done ) &
KEEPALIVE_PID=$!
trap 'kill $KEEPALIVE_PID 2>/dev/null || true' EXIT

# ---- samplers ---------------------------------------------------------------
sampler() {  # $1 container, $2 csv — cols: ts,cpu_usec,mem_current,pids,anon,mem_peak,file_cache
  ( echo "ts,cpu_usage_usec,mem_current,pids,anon_bytes,mem_peak,file_cache"
    while docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; do
      line=$(docker exec "$1" sh -c \
        'awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/pids.current 2>/dev/null || echo 0; awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null || echo 0; cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0; awk "/^file /{print \$2}" /sys/fs/cgroup/memory.stat 2>/dev/null || echo 0' \
        2>/dev/null | tr '\n' ',') || line=""
      [ -n "$line" ] && echo "$(date +%s),${line%,}"
      sleep 15
    done ) > "$2" &
  echo $!
}
# Container-side snapshot snippets (python processes only: the snapshot's own
# sh/tr/cut children must not count as "new" processes). Plain variables — a
# `case … )` inside $( ) trips bash 3.2's parser.
# Every process except the snapshot shell itself ($$) and its children (xargs/
# cut/awk) — the first smoke's "*python*" filter matched the snapshot's OWN
# sh -c (its command text contains "python") and nothing else. NUL-separated
# cmdline/environ go through xargs -0 (no backslash escapes to mangle).
SNAP_SH='for p in /proc/[0-9]*; do pid=${p#/proc/}; [ "$pid" = "$$" ] && continue; pp=$(awk "{print \$4}" $p/stat 2>/dev/null); [ "$pp" = "$$" ] && continue; echo "$pid|ppid=$pp $(xargs -0 echo < $p/cmdline 2>/dev/null | cut -c1-160)"; done'
ENV_SH='for p in /proc/[0-9]*; do pid=${p#/proc/}; [ "$pid" = "$$" ] && continue; pp=$(awk "{print \$4}" $p/stat 2>/dev/null); [ "$pp" = "$$" ] && continue; e=$(xargs -0 -n1 echo < $p/environ 2>/dev/null | grep -E "^(OMP_NUM_THREADS|MKL_NUM_THREADS|OPENBLAS_NUM_THREADS|VECLIB_MAXIMUM_THREADS|NUMEXPR_NUM_THREADS|TORCH_NUM_THREADS)=" | tr "\n" ";"); echo "$pid|$e"; done'
RSS_SH='for p in /proc/[0-9]*; do pid=${p#/proc/}; [ "$pid" = "$$" ] && continue; pp=$(awk "{print \$4}" $p/stat 2>/dev/null); [ "$pp" = "$$" ] && continue; echo "$pid $(grep VmRSS $p/status 2>/dev/null | awk "{print \$2}") $(xargs -0 echo < $p/cmdline 2>/dev/null | cut -c1-60)"; done'
rss_sampler() {  # $1 container, $2 log — per-PID RSS of python processes, every 60 s
  ( while docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; do
      echo "== $(date +%s)"
      docker exec "$1" sh -c "$RSS_SH" 2>/dev/null || true
      sleep 60
    done ) > "$2" &
  echo $!
}
driver_sampler() {  # $1 csv — waits for the smoke-run container, then samples it (driver CPU measured separately)
  ( c=""; for i in $(seq 1 120); do c=$(docker ps --filter name=smoke-run --format '{{.Names}}' | head -1); [ -n "$c" ] && break; sleep 1; done
    [ -n "$c" ] && sampler "$c" "$1" >/dev/null ) &
}
census_watcher() {  # $1 container, $2 census dir — answers <seq>.request with <seq>.json
  ( while :; do
      for req in "$2"/*.request; do
        [ -e "$req" ] || { sleep 0.5; continue; }
        seq=$(basename "$req" .request)
        want_env=$(python3 -c "import json,sys; print(int(json.load(open(sys.argv[1])).get('want_env', False)))" "$req")
        snap=$(docker exec "$1" sh -c "$SNAP_SH" 2>/dev/null || true)
        envs=""
        if [ "$want_env" = "1" ]; then
          envs=$(docker exec "$1" sh -c "$ENV_SH" 2>/dev/null || true)
        fi
        python3 - "$seq" "$2" "$snap" "$envs" <<'PY'
import json, os, sys
seq, d, snap, envs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
pids, cmd, env = [], {}, {}
for line in snap.splitlines():
    if "|" in line:
        p, c = line.split("|", 1)
        if p.isdigit():
            pids.append(int(p)); cmd[p] = c
for line in envs.splitlines():
    if "|" in line:
        p, e = line.split("|", 1)
        env[p] = dict(kv.split("=", 1) for kv in e.split(";") if "=" in kv)
json.dump({"seq": int(seq), "pids": sorted(pids), "cmd": cmd, "env": env},
          open(f"{d}/{seq}.json.tmp", "w"))
os.replace(f"{d}/{seq}.json.tmp", f"{d}/{seq}.json")
PY
        rm -f "$req"
      done
    done ) > "$2/watcher.log" 2>&1 &     # MUST redirect: a backgrounded loop that
  echo $!                                 # keeps this function's stdout open hangs
}                                         # the CW=$(...) capture forever (bitten)

echo "== [1/6] quiet-box preflight"
[ "$(docker ps -q | wc -l | tr -d ' ')" = "0" ] || { echo "FATAL: containers running — box not quiet" >&2; docker ps; exit 1; }
load=$(cut -d' ' -f1 /proc/loadavg); echo "   load1=$load (keepalive contributes ~1.0)"

echo "== [2/6] corpus: AMI first $N measured (committed order) + last $NWARM as disjoint warm"
[ -f "$SRC/corpus_manifest.json" ] || { echo "FATAL: no corpus manifest in $SRC" >&2; exit 1; }
python3 - "$PIN" "$SRC" "$M8" "$N" "$NWARM" "$CORPUS_EXT" <<'PY'
import json, os, sys
pin, src, m8, n, nwarm = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
ext = sys.argv[6] if len(sys.argv) > 6 else ".avi"
docs = []
for line in open(pin):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    # Column 1 is the archive id and matches the manifest key stem exactly.
    # Column 2 is the UPSTREAM archive.org source filename and differs from the
    # staged filename for 213 of the 500 films — do not use it.
    docs.append(line.split("\t")[0] + ext)
assert len(docs) >= n + nwarm, f"pin has {len(docs)} docs, need {n + nwarm}"
measured, warm = docs[:n], docs[-nwarm:]
assert not set(measured) & set(warm)
for sub, names in (("measured", measured), ("warm", warm)):
    d = os.path.join(m8, sub); os.makedirs(d, exist_ok=True)
    for nm in names:
        s, t = os.path.join(src, nm), os.path.join(d, nm)
        assert os.path.exists(s), f"missing on disk: {nm}"
        if not os.path.exists(t): os.link(s, t)
    for extra in os.listdir(d):
        if extra.endswith(ext) and extra not in names:
            os.unlink(os.path.join(d, extra))
open(os.path.join(m8, "measured", "measured_order.txt"), "w").write("\n".join(measured) + "\n")
m = json.load(open(os.path.join(src, "corpus_manifest.json")))
json.dump(m, open(os.path.join(m8, "measured", "corpus_manifest.json"), "w"))
json.dump(m, open(os.path.join(m8, "warm", "corpus_manifest.json"), "w"))
vd = m.get("video_duration_s", {})
print(f"   measured {len(measured)} docs, {sum(vd.get(d, 0) for d in measured)/3600:.2f} h probed; warm {warm}")
PY
echo "== [3/6] hash verification of measured files (once, before either arm)"
( cd "$M8/measured" && xargs -P 8 -n 1 sha256sum < measured_order.txt ) > "$OUT/preflight_hashes.txt"
python3 - "$M8/measured/corpus_manifest.json" "$OUT/preflight_hashes.txt" "$M8/measured" "$OUT/preflight_hashes.json" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1])); shas = m["sha256"]; ok, bad = {}, []
for line in open(sys.argv[2]):
    h, name = line.split(); want = shas.get(name, {})
    nb = os.path.getsize(os.path.join(sys.argv[3], name))
    if want.get("sha256") != h: bad.append(f"{name}: sha mismatch")
    elif want.get("bytes") and want["bytes"] != nb: bad.append(f"{name}: bytes mismatch")
    else: ok[name] = {"sha256": h, "bytes": nb}
if bad: sys.exit("PREFLIGHT HASH FAIL: " + "; ".join(bad[:5]))
json.dump({"verified": ok, "n": len(ok)}, open(sys.argv[4], "w"))
print(f"   {len(ok)} files verified against the AMI manifest")
PY

echo "== [4/6] build + provenance"
# Build only the arms this run actually uses. A RocketRide-only campaign must not
# be blocked by a LangGraph image rebuild (torch + CUDA, ~6 GB) — that crashed the
# 2026-09-02 rr_best_configs launch with a buildkit RPC EOF on a target it never ran.
BUILD_TARGETS="smoke"
case " $ARM_ORDER " in *" rr "*) BUILD_TARGETS="rocketride $BUILD_TARGETS" ;; esac
case " $ARM_ORDER " in *" lg "*|*" lg1 "*|*" lgip "*) BUILD_TARGETS="langgraph $BUILD_TARGETS" ;; esac
echo "   build targets: $BUILD_TARGETS  (ARM_ORDER=$ARM_ORDER)"
docker compose build $BUILD_TARGETS
PROV="$OUT/provenance"
{ git rev-parse HEAD; git describe --always --dirty 2>/dev/null || true; } > "$PROV/git_state.txt" 2>/dev/null || true
{ uname -a; nproc; lscpu 2>/dev/null | head -12; docker compose version; } > "$PROV/host.txt" || true
for img in $(docker compose config --images 2>/dev/null | sort -u); do
  docker image inspect "$img" --format '{{.RepoTags}} id={{.Id}} digests={{.RepoDigests}} created={{.Created}}' >> "$PROV/images.txt" 2>/dev/null || true
done
docker compose run --rm --no-deps --entrypoint pip langgraph freeze > "$PROV/lg_pip_freeze.txt" 2>/dev/null || true
docker compose run --rm --no-deps --entrypoint python langgraph -V > "$PROV/lg_python_version.txt" 2>/dev/null || true
docker compose run --rm --no-deps --entrypoint sh langgraph -c 'ffmpeg -version | head -1; sha256sum $(command -v ffmpeg)' > "$PROV/lg_ffmpeg.txt" 2>/dev/null || true
docker compose run --rm --no-deps --entrypoint pip smoke show rocketride 2>/dev/null | grep -E "^Version" > "$PROV/rr_sdk_version.txt" || true
sha256sum arms/rocketride/Dockerfile > "$PROV/rr_dockerfile.sha256" || true
cp "$PIN" "$M8/measured/measured_order.txt" "$PROV/" || true
{ echo "POSTURE=matched${RR_TASKS}x${RR_BLAS_THREADS}_native ARM_ORDER=$ARM_ORDER REP=$REP N=$N WARM=$NWARM RR_TASKS=$RR_TASKS RR_THREADS_PER_CONN=${RR_THREADS_PER_CONN:-OMITTED} RR_INFLIGHT_PER_TASK=$RR_INFLIGHT_PER_TASK";
  echo "RR: tasks=8 threads_arg=OMITTED(engine default item threads 64) ttl=0 BLAS/OMP=4 mem=58g sharded_blast";
  echo "LG: workers=8 ports=8201-8208 torch=4/1 detect_concurrency=1 c32=8x4 mem=58g";
  echo "BENCH_TIMEOUT_S=$BENCH_TIMEOUT_S"; } > "$PROV/run_config.txt"

( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID $KEEPALIVE_PID 2>/dev/null || true' EXIT

rc_rr=0; rc_lg=0
run_rr() {
  echo "== ARM RocketRide rr_matched_${RR_TASKS}x${RR_BLAS_THREADS} (sharded blast, $RR_TASKS tasks, threads=${RR_THREADS_PER_CONN:-OMITTED}, inflight=$RR_INFLIGHT_PER_TASK, $N docs, warm x$RR_TASKS)"
  docker compose up -d rocketride-matched
  for i in $(seq 1 90); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride-m8 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 90 ] && { echo "FATAL: RR engine never healthy"; exit 1; }
    sleep 5
  done
  S1=$(sampler videobench-rocketride-m8 "$OUT/rr/engine_cgroup.csv")
  S2=$(rss_sampler videobench-rocketride-m8 "$OUT/rr/rss_by_pid.log")
  CW=$(census_watcher videobench-rocketride-m8 "$OUT/rr/census")
  driver_sampler "$OUT/rr/driver_cgroup.csv"
  CORPUS="$M8/measured" WARM="$M8/warm" docker compose run --rm smoke \
    python /bench/bench_video_matched.py /corpus "/results/$RUN/rr" "$N" /warm \
    > "$OUT/rr/driver.log" 2>&1 || rc_rr=$?
  kill "$S1" "$S2" "$CW" 2>/dev/null || true
  docker compose logs --no-color rocketride-matched > "$OUT/rr/service.log" 2>&1 || true
  docker compose stop rocketride-matched && docker compose rm -f rocketride-matched
  echo "   RR done (rc=$rc_rr)"
}
run_lg() {
  echo "== ARM LangGraph lg_matched_8x4_c32 (8 uvicorn procs, c32=8x4, $N docs, warm x8)"
  docker compose up -d langgraph-matched
  for i in $(seq 1 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph-m8 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 120 ] && { echo "FATAL: LG workers never all ready"; docker compose logs --no-color langgraph-matched | tail -40; exit 1; }
    sleep 5
  done
  S1=$(sampler videobench-langgraph-m8 "$OUT/lg/engine_cgroup.csv")
  S2=$(rss_sampler videobench-langgraph-m8 "$OUT/lg/rss_by_pid.log")
  driver_sampler "$OUT/lg/driver_cgroup.csv"
  CORPUS="$M8/measured" WARM="$M8/warm" docker compose run --rm smoke \
    python /bench/lg_driver_matched.py /corpus "/results/$RUN/lg" "$N" /warm \
    > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
  kill "$S1" "$S2" 2>/dev/null || true
  docker compose logs --no-color langgraph-matched > "$OUT/lg/service.log" 2>&1 || true
  docker compose stop langgraph-matched && docker compose rm -f langgraph-matched
  echo "   LG done (rc=$rc_lg)"
}
run_lg1() {  # ONE-PORT cell: uvicorn --workers 8 on 8200, kernel-balanced
  echo "== ARM LangGraph lg_workers8_1port_c32 (uvicorn --workers 8, one port, c32, $N docs)"
  docker compose up -d langgraph-workers8
  for i in $(seq 1 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph-w8 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 120 ] && { echo "FATAL: LG one-port service never ready"; docker compose logs --no-color langgraph-workers8 | tail -40; exit 1; }
    sleep 5
  done
  S1=$(sampler videobench-langgraph-w8 "$OUT/lg/engine_cgroup.csv")
  S2=$(rss_sampler videobench-langgraph-w8 "$OUT/lg/rss_by_pid.log")
  driver_sampler "$OUT/lg/driver_cgroup.csv"
  CORPUS="$M8/measured" WARM="$M8/warm" LG_URL="http://langgraph-workers8:8200" docker compose run --rm smoke \
    python /bench/lg_driver_1port.py /corpus "/results/$RUN/lg" "$N" /warm \
    > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
  kill "$S1" "$S2" 2>/dev/null || true
  docker compose logs --no-color langgraph-workers8 > "$OUT/lg/service.log" 2>&1 || true
  docker compose stop langgraph-workers8 && docker compose rm -f langgraph-workers8
  echo "   LG one-port done (rc=$rc_lg)"
}
run_lgip() {  # IN-PROCESS cell: one uvicorn, one model copy, LG_EXPECT_DETECT_CONC concurrent predicts x LG_TORCH_THREADS
  echo "== ARM LangGraph in-process (1 uvicorn, $LG_EXPECT_DETECT_CONC predicts x $LG_TORCH_THREADS threads, c$LG_PER_ENDPOINT_CONCURRENCY, $N docs)"
  docker compose up -d langgraph-inproc
  for i in $(seq 1 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph-ip 2>/dev/null)" = "healthy" ] && break
    [ "$i" = 120 ] && { echo "FATAL: LG in-process service never ready"; docker compose logs --no-color langgraph-inproc | tail -40; exit 1; }
    sleep 5
  done
  S1=$(sampler videobench-langgraph-ip "$OUT/lg/engine_cgroup.csv")
  S2=$(rss_sampler videobench-langgraph-ip "$OUT/lg/rss_by_pid.log")
  driver_sampler "$OUT/lg/driver_cgroup.csv"
  CORPUS="$M8/measured" WARM="$M8/warm" LG_HOST=langgraph-inproc LG_PORT_BASE=8200 LG_WORKERS=1 \
    docker compose run --rm smoke \
    python /bench/lg_driver_matched.py /corpus "/results/$RUN/lg" "$N" /warm \
    > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
  kill "$S1" "$S2" 2>/dev/null || true
  docker compose logs --no-color langgraph-inproc > "$OUT/lg/service.log" 2>&1 || true
  docker compose stop langgraph-inproc && docker compose rm -f langgraph-inproc
  echo "   LG in-process done (rc=$rc_lg)"
}
echo "== [5/6] arms in order: $ARM_ORDER"
# Keepalive PAUSED for the arms phase: the box is busy (boot, warm-up, blast),
# so the idle watchdog is no risk, and the busy loop must not tax measured spans.
kill -STOP "$KEEPALIVE_PID" 2>/dev/null || true
for arm in $ARM_ORDER; do
  case "$arm" in rr) run_rr;; lg) run_lg;; lg1) run_lg1;; lgip) run_lgip;; *) echo "FATAL: bad ARM_ORDER $arm" >&2; exit 1;; esac
  sleep 20   # settle between arms
done
kill -CONT "$KEEPALIVE_PID" 2>/dev/null || true

echo "== [6/6] report + final sync"
# Single-arm runs (ARM_ORDER=rr or lg) get a single-arm report; PAIR_RR / PAIR_LG
# point at an earlier arm directory to produce the paired cross-arm report later.
RR_DIR="$OUT/rr"; LG_DIR="$OUT/lg"
[ -n "${PAIR_RR:-}" ] && { RR_DIR="$PAIR_RR"; cp -r "$PAIR_RR" "$OUT/rr_paired"; }
[ -n "${PAIR_LG:-}" ] && { LG_DIR="$PAIR_LG"; cp -r "$PAIR_LG" "$OUT/lg_paired"; }
rc_rep=0
if [ -f "$RR_DIR/per_doc.jsonl" ] && [ -f "$LG_DIR/per_doc.jsonl" ]; then
  python3 bench/report.py --arms "$RR_DIR" "$LG_DIR" > "$OUT/report.txt" 2>&1 || rc_rep=$?
elif [ -f "$RR_DIR/per_doc.jsonl" ]; then
  echo "   single-arm report: RocketRide only (pair later with PAIR_RR=$OUT/rr)"
  python3 bench/report.py "$RR_DIR" > "$OUT/report.txt" 2>&1 || rc_rep=$?
elif [ -f "$LG_DIR/per_doc.jsonl" ]; then
  echo "   single-arm report: LangGraph only (pair later with PAIR_LG=$OUT/lg)"
  python3 bench/report.py "$LG_DIR" > "$OUT/report.txt" 2>&1 || rc_rep=$?
else
  echo "   no arm produced records" > "$OUT/report.txt"; rc_rep=1
fi
cat "$OUT/report.txt"
kill $SYNC_PID 2>/dev/null || true
rc_sync=0
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet || rc_sync=$?
echo "== FINAL STATUS $RUN: rc_rr=$rc_rr rc_lg=$rc_lg rc_report=$rc_rep rc_sync=$rc_sync"
overall=0
for rc in "$rc_rr" "$rc_lg" "$rc_rep" "$rc_sync"; do [ "$rc" -ne 0 ] && overall=1; done
[ "$overall" -eq 0 ] && echo "   execution PASS + validity PASS (matched8x4_native, single rep = SIZING)" \
                     || echo "   FAILURE — inspect the nonzero component; report: $OUT/report.txt"
exit "$overall"
