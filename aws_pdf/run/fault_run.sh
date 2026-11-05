#!/usr/bin/env bash
# FAULT-INJECTION run — M4 blast radius + M5 fault isolation.
#
# Deliberately SEPARATE from matched_run.sh: exception paths and retries change
# timing, so mixing this with a throughput run contaminates both numbers.
#
# Per arm, in order:
#   1. resource baseline BEFORE any fault (M5's rss_growth needs a pre-FAULT
#      baseline; a run-boundary baseline would conflate warm-up growth)
#   2. the fault corpus: N clean docs with poison at known positions
#   3. a RECOVERY PROBE -- one clean doc AFTER the faults. This is the real
#      test: a service can answer a health check while being unable to process
#      anything.
#   4. if the driver failed or recovery failed: capture diagnostics FIRST,
#      then exactly ONE controlled restart, then re-probe. restart_required is
#      recorded, never inferred.
#
#   bash run/fault_run.sh          # MODE=c8 default, then rerun with MODE=native_saturation
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MODE="${MODE:-c8}"
RUN="$HERE/results/fault_${STAMP}_${MODE}"
N_CLEAN="${N_CLEAN:-200}"
POSITIONS="${POSITIONS:-2,67,134,199}"
CLEAN_SRC="${CLEAN_SRC:-$HOME/bench_corpus_n1000_off200}"
CORPUS="$HOME/fault_corpus_${N_CLEAN}"
WARM="${WARM:-0}"          # no warm-up: it would consume the clean docs
LG=bench-langgraph; RR=bench-rocketride
HOSTCPUS="$(nproc)"; ARM_CPUS="${ARM_CPUS:-$(( HOSTCPUS * 3 / 4 ))}"
_n=${ARM_CPUS%%.*}
: "${BENCH_CPUSET:=0-$(( _n - 1 ))}"; : "${CLIENT_CPUSET:=${_n}-$(( HOSTCPUS - 1 ))}"
export ARM_CPUS BENCH_CPUSET CLIENT_CPUSET CORPUS
export LG_EXTRACTOR="${LG_EXTRACTOR:-tika}" RR_DUP_PATCH="${RR_DUP_PATCH:-1}"
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-900}"
mkdir -p "$RUN"
REL="$(basename "$RUN")"
say() { echo "[fault $(date -u +%H:%M:%S)] $*"; }

if [ "$MODE" = "native_saturation" ]; then
  LG_MODE="c${LG_CLIENT_WINDOW:-128}"; RR_MODE="blast"
else LG_MODE="$MODE"; RR_MODE="$MODE"; fi

[ "$(uname -m)" = x86_64 ] || { echo "FATAL: need x86_64"; exit 1; }
[ -d "$CLEAN_SRC" ] || { echo "FATAL: no clean corpus at $CLEAN_SRC"; exit 1; }

say "building fault corpus: $N_CLEAN clean + 4 poison at $POSITIONS"
python3 corpus/make_faults.py "$CLEAN_SRC" "$CORPUS" "$N_CLEAN" "$POSITIONS"
cp "$CORPUS/fault_manifest.json" "$RUN/"
cp "$CORPUS/SHA256SUMS" "$RUN/corpus.sha256"
N_TOTAL=$(find "$CORPUS" -name '*.pdf' | wc -l | tr -d ' ')

{ echo "stamp_utc=$STAMP"; echo "arch=$(uname -m)"; echo "nproc=$HOSTCPUS"
  echo "mem_gb=$(awk '/MemTotal/{printf "%.0f",$2/1048576}' /proc/meminfo)"
  echo "os=$(. /etc/os-release && echo "$PRETTY_NAME")"; echo "kernel=$(uname -r)"
  echo "docker=$(docker version --format '{{.Server.Version}}')"
  echo "git_sha=$(git rev-parse HEAD)"; echo "git_dirty=$(git status --porcelain | wc -l | tr -d ' ')"
  echo "corpus=$CORPUS"; echo "n_docs=$N_TOTAL"; echo "n_clean=$N_CLEAN"
  echo "fault_positions=$POSITIONS"; echo "reps=1"; echo "mode=$MODE"
  echo "lg_mode=$LG_MODE"; echo "rr_mode=$RR_MODE"; echo "warm_docs=$WARM"
  echo "arm_cpus=$ARM_CPUS"; echo "arm_mem=UNCAPPED"; echo "bench_cpuset=$BENCH_CPUSET"
  echo "client_cpuset=$CLIENT_CPUSET"; echo "timeout_s=$BENCH_TIMEOUT_S"
  echo "lg_extractor=$LG_EXTRACTOR"; echo "rr_threads=${RR_THREADS:-NONE (engine default)}"
  echo "rr_dup_patch=$RR_DUP_PATCH"; echo "rr_engine_version=3.3.1"; echo "rr_sdk_version=1.3.0"
  echo "run_kind=fault_injection"
} > "$RUN/environment.txt"

docker compose build langgraph rocketride 2>&1 | tail -4
docker compose --profile client build bench 2>&1 | tail -2
for i in langgraph rocketride; do
  docker image inspect "bench-$i:latest" --format "$i {{.Id}} {{.Architecture}}" >> "$RUN/image_ids.txt"
done

wait_healthy() {
  local s
  for _ in $(seq 1 60); do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || echo none)
    [ "$s" = healthy ] && return 0
    sleep 5
  done
  return 1
}

# One clean document, never part of the fault corpus, used as the probe.
PROBE_DIR="$HOME/fault_probe"; mkdir -p "$PROBE_DIR"; rm -f "$PROBE_DIR"/*.pdf
cp "$(ls "$CLEAN_SRC"/*.pdf | tail -1)" "$PROBE_DIR/"

sample() {  # $1 container, $2 outfile -- a short burst, not a stream
  timeout 12 docker exec -e SAMPLE_MAX_S=10 -i "$1" python3 - \
    < bench/cgroup_sampler.py > "$2" 2>/dev/null || true
}

run_arm() {   # $1 arm-key  $2 container  $3 driver  $4 mode
  # Declared separately on purpose: referencing $arm inside the same `local`
  # statement that declares it trips `set -u`.
  local arm="$1" cont="$2" drv="$3" md="$4"
  local D="$RUN/$arm/rep1"
  mkdir -p "$D"
  say "=== $arm: mode=$md, $N_TOTAL docs (incl 4 poison) ==="
  if [ "$arm" = lg ] && [ "$LG_EXTRACTOR" = tika ]; then docker compose up -d tika; fi
  docker compose up -d "$( [ "$arm" = lg ] && echo langgraph || echo rocketride )"
  wait_healthy "$cont" || { say "FATAL: $cont never healthy"; docker logs --tail 40 "$cont"; return 1; }

  say "$arm: PRE-FAULT resource baseline"
  sample "$cont" "$D/resources_pre_fault.jsonl"

  docker exec -e SAMPLE_MAX_S=1800 -i "$cont" python3 - < bench/cgroup_sampler.py \
    > "$D/sampler.jsonl" 2>"$D/sampler.err" &
  local SP=$!
  sleep 2
  set +e
  docker compose --profile client run --rm bench \
    "/bench/$drv" /corpus "/results/$REL/$arm/rep1" "$N_TOTAL" "$md" "$WARM"
  local DRV=$?
  set -e
  sleep 2
  kill "$SP" 2>/dev/null || true; wait "$SP" 2>/dev/null || true
  echo "$DRV" > "$D/driver_rc.txt"
  say "$arm: driver rc=$DRV"

  say "$arm: POST-FAULT resource baseline"
  sample "$cont" "$D/resources_post_fault.jsonl"

  # RECOVERY PROBE: a health endpoint answering is not the same as a service
  # that can still process a document.
  say "$arm: recovery probe (1 clean doc, unrelated to the corpus)"
  mkdir -p "$D/recovery"
  set +e
  docker run --rm --network aws_pdf_default \
    -v "$PROBE_DIR:/corpus:ro" -v "$HERE/results:/results" -v "$HERE/pipe:/pipe:ro" \
    -v "$HERE/arms/rocketride/data/probe:/probe:ro" \
    -e LG_URL=http://langgraph:8100 -e ROCKETRIDE_URI=ws://rocketride:5565/task/service \
    -e ROCKETRIDE_APIKEY=local-dev -e BENCH_PIPE=/pipe/benchmark_pdf.pipe \
    -e BENCH_WARMUP_DOC=/probe/sample.pdf -e BENCH_TIMEOUT_S=300 \
    --cpuset-cpus "$CLIENT_CPUSET" bench-client:latest \
    "/bench/$drv" /corpus "/results/$REL/$arm/rep1/recovery" 1 seq 0
  local PROBE1=$?
  set -e
  echo "$PROBE1" > "$D/recovery_rc_before_restart.txt"
  say "$arm: recovery rc=$PROBE1"

  local RESTARTED=false
  if [ "$DRV" -ne 0 ] || [ "$PROBE1" -ne 0 ]; then
    say "$arm: DEGRADED — capturing diagnostics BEFORE touching the service"
    docker logs --tail 400 "$cont" > "$D/diag_logs.txt" 2>&1 || true
    docker exec "$cont" sh -c 'ps ax -o pid,rss,nlwp,stat,args | head -30' \
      > "$D/diag_ps.txt" 2>&1 || true
    docker exec "$cont" sh -c 'cat /sys/fs/cgroup/memory.stat 2>/dev/null | head -8; \
      echo ---; cat /sys/fs/cgroup/pids.current 2>/dev/null' \
      > "$D/diag_cgroup.txt" 2>&1 || true
    docker stats --no-stream "$cont" > "$D/diag_stats.txt" 2>&1 || true

    say "$arm: ONE controlled restart"
    docker compose restart "$( [ "$arm" = lg ] && echo langgraph || echo rocketride )" \
      >/dev/null 2>&1 || true
    RESTARTED=true
    wait_healthy "$cont" || say "$arm: WARNING still unhealthy after restart"
    mkdir -p "$D/recovery_after_restart"
    set +e
    docker run --rm --network aws_pdf_default \
      -v "$PROBE_DIR:/corpus:ro" -v "$HERE/results:/results" -v "$HERE/pipe:/pipe:ro" \
      -v "$HERE/arms/rocketride/data/probe:/probe:ro" \
      -e LG_URL=http://langgraph:8100 -e ROCKETRIDE_URI=ws://rocketride:5565/task/service \
      -e ROCKETRIDE_APIKEY=local-dev -e BENCH_PIPE=/pipe/benchmark_pdf.pipe \
      -e BENCH_WARMUP_DOC=/probe/sample.pdf -e BENCH_TIMEOUT_S=300 \
      --cpuset-cpus "$CLIENT_CPUSET" bench-client:latest \
      "/bench/$drv" /corpus "/results/$REL/$arm/rep1/recovery_after_restart" 1 seq 0
    echo "$?" > "$D/recovery_rc_after_restart.txt"
    set -e
  fi
  echo "$RESTARTED" > "$D/restart_required.txt"
  docker compose stop "$( [ "$arm" = lg ] && echo langgraph || echo rocketride )" \
    tika >/dev/null 2>&1 || true
}

run_arm lg "$LG" lg_driver.py "$LG_MODE" || say "lg arm ended with an error"
run_arm rr "$RR" rr_driver.py "$RR_MODE" || say "rr arm ended with an error"

python3 run/write_provenance.py "$RUN" || true
say "deriving M4/M5"
set +e
python3 bench/fault_report.py "$RUN" | tee "$RUN/fault_report.txt"
RC=${PIPESTATUS[0]}
set -e

S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench}/$REL/"
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  aws s3 cp "$RUN/" "$S3" --recursive --only-show-errors && say "exfil -> $S3"
fi
say "results in $RUN"
exit "$RC"
