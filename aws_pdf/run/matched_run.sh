#!/usr/bin/env bash
# THE matched benchmark run: both arms, identical in every respect except the
# framework. This is the only kind of run whose numbers may be compared.
#
# Held identical across arms:
#   same corpus, same N, same document order   (govdocs, sorted[:N])
#   same envelope                              (ARM_CPUS / ARM_MEM, both arms)
#   same rep count                             (REPS)
#   same mode                                  (blast, c<N> closed-loop, or b<B> batches)
#   same warm-start policy                     (WARM docs, excluded)
#   arms run ONE AT A TIME                     (no contention)
#
# Only the framework varies. Anything else that differs invalidates the
# comparison, which is why every one of these is recorded in provenance.json.
#
#   bash run/matched_run.sh                 # blast, defaults below
#   MODE=c8 REPS=3 bash run/matched_run.sh  # closed-loop at 8 in flight
#   MODE=b32 SKIP_LG=1 bash run/matched_run.sh  # RocketRide: send_files batches of 32
#   MODE=b32 SKIP_RR=1 bash run/matched_run.sh  # LangGraph: waves of 32, barrier between
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
# The current AWS campaign defaults to a verified official release. Explicit
# engine selections (including historical/experimental A/B arms) take priority.
if [ -z "${ENGINE_URL:-}" ] && [ -z "${ENGINE_VERSION:-}" ]; then
  . "$HERE/run/official-engine-20261021.env"
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="$HERE/results/${RUN_TAG:-${STAMP}_${MODE:-blast}}"
N="${N:-200}"; REPS="${REPS:-3}"; WARM="${WARM:-25}"; MODE="${MODE:-blast}"
# native_saturation: each arm runs its OWN native ingestion path, because they
# are not the same interface. LangGraph is an HTTP service kept supplied by a
# bounded client window; RocketRide takes the whole backlog in one SDK batch
# and schedules it internally. This is a saturation comparison, NOT an
# equal-submission-interface or equal-thread-count comparison. Both arms are
# still held to the SAME cpuset, which is where fairness actually lives.
LG_CLIENT_WINDOW="${LG_CLIENT_WINDOW:-128}"
if [ "$MODE" = "native_saturation" ]; then
  LG_MODE="c${LG_CLIENT_WINDOW}"
  RR_MODE="blast"
else
  LG_MODE="$MODE"; RR_MODE="$MODE"
fi
# Corpus dir is derived FROM N, so the two cannot drift apart. Override
# CORPUS only to point at a deliberately different document set.
# CORPUS_OFFSET selects a DISJOINT document set. The dir name encodes N and
# the offset, so a run can never pick up a corpus built for other parameters.
CORPUS_OFFSET="${CORPUS_OFFSET:-0}"
CORPUS="${CORPUS:-$HOME/bench_corpus_n${N}_off${CORPUS_OFFSET}}"
# Core split. The arm under test gets ARM_CPUS cores; the bench CLIENT gets
# the remainder, on its OWN cores, so it can never steal from the arm it is
# measuring. Every container in an arm shares the arm's set -- LangGraph's arm
# is langgraph+tika together, RocketRide's is one container -- otherwise the
# LG arm silently gets a second full allocation.
HOSTCPUS="$(nproc)"
ARM_CPUS="${ARM_CPUS:-$(( HOSTCPUS * 3 / 4 ))}"
_n=${ARM_CPUS%%.*}
: "${BENCH_CPUSET:=0-$(( _n - 1 ))}"
# When the arm takes the WHOLE host there are no cores left to isolate the
# client onto; the default would compute an inverted range (e.g. "32-31") and
# docker rejects it. Fall back to sharing the full host -- the client then
# contends with the arm it is measuring, so record that, never imply it.
if [ "$_n" -ge "$HOSTCPUS" ]; then
  : "${CLIENT_CPUSET:=0-$(( HOSTCPUS - 1 ))}"
  CLIENT_ISOLATED=false
else
  : "${CLIENT_CPUSET:=${_n}-$(( HOSTCPUS - 1 ))}"
  CLIENT_ISOLATED=true
fi
export ARM_CPUS BENCH_CPUSET CLIENT_CPUSET CLIENT_ISOLATED
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-3600}"
# In-container sampler lifetime. It used to be a hardcoded 7200 s, which
# silently truncated CPU/RSS coverage for any rep longer than two hours (the
# 2026-09-16 b16 cell ran 18,290 s). Default is now a day; the report's
# `coverage` block discloses any window the sampler did not fully cover.
export SAMPLE_MAX_S="${SAMPLE_MAX_S:-86400}"
# Memory is measured, not capped -- see docker-compose.yml. A 10g cap would
# have OOM-killed RocketRide (peak 10,536 MB) and capping per container gave
# the two-container LG arm twice RocketRide's ceiling.
export CORPUS
export RR_DUP_PATCH="${RR_DUP_PATCH:-1}"
export CENSUS_EMPTY_POLICY="${CENSUS_EMPTY_POLICY:-fail}"
export LG_EXTRACTOR="${LG_EXTRACTOR:-tika}"
LG=bench-langgraph; RR=bench-rocketride
REL="$(basename "$RUN")"          # results/ is mounted at /results in the client
mkdir -p "$RUN"
say() { echo "[bench $(date -u +%H:%M:%S)] $*"; }

# ------------------------------------------------------------------- gates
[ "$(uname -m)" = x86_64 ] || { echo "FATAL: need x86_64 (timings are invalid under emulation)"; exit 1; }
# Fetch on demand: idempotent, and a corpus for this N either exists
# complete and verified or is rebuilt. No silent partial corpora.
bash corpus/fetch_govdocs.sh "$N" "$CORPUS" "$CORPUS_OFFSET" "$WARM"
have=$(find "$CORPUS" -name '*.pdf' | wc -l | tr -d ' ')
# N measured + WARM disjoint warm-up documents.
[ "$have" -eq "$(( N + WARM ))" ] || { echo "FATAL: corpus has $have PDFs, expected $(( N + WARM ))"; exit 1; }
(cd "$CORPUS" && sha256sum -c --quiet SHA256SUMS) || { echo "FATAL: corpus checksum mismatch"; exit 1; }

{ echo "stamp_utc=$STAMP"; echo "arch=$(uname -m)"; echo "nproc=$(nproc)"
  echo "mem_gb=$(awk '/MemTotal/{printf "%.0f",$2/1048576}' /proc/meminfo)"
  echo "os=$(. /etc/os-release && echo "$PRETTY_NAME")"
  echo "kernel=$(uname -r)"
  echo "docker=$(docker version --format '{{.Server.Version}}')"
  echo "git_sha=$(git rev-parse HEAD)"
  echo "git_dirty=$(git status --porcelain | wc -l | tr -d ' ')"
  echo "corpus=$CORPUS"; echo "n_docs=$N"; echo "reps=$REPS"; echo "mode=$MODE"
  echo "corpus_offset=$CORPUS_OFFSET"
  echo "lg_mode=$LG_MODE"; echo "rr_mode=$RR_MODE"
  echo "lg_client_window=$LG_CLIENT_WINDOW"
  echo "warm_docs=$WARM"; echo "arm_cpus=$ARM_CPUS"
  echo "arm_mem=UNCAPPED (measured, not enforced)"
  echo "bench_cpuset=$BENCH_CPUSET"; echo "client_cpuset=$CLIENT_CPUSET"
  echo "client_isolated=$CLIENT_ISOLATED"
  echo "census_empty_policy=${CENSUS_EMPTY_POLICY:-fail}"
  echo "timeout_s=$BENCH_TIMEOUT_S"; echo "client=containerized (bench-client)"
  echo "lg_extractor=$LG_EXTRACTOR"
  echo "rr_threads=${RR_THREADS:-NONE (engine default)}"
  echo "rr_dup_patch=$RR_DUP_PATCH"; echo "rr_engine_version=${ENGINE_VERSION:-3.3.1}"
  echo "rr_engine_url=${ENGINE_URL:-release 3.3.1}"; echo "rr_parse_stock_url=${PARSE_STOCK_URL:-none}"
  echo "rr_sdk_version=1.3.0"
  echo "rr_engine_archive_sha256=${ENGINE_ARCHIVE_SHA256:-unrecorded}"
  echo "rr_profile_seconds=${RR_PROFILE_SECONDS:-0}"
} > "$RUN/environment.txt"
cp "$CORPUS/SHA256SUMS" "$RUN/corpus.sha256"
cp "$CORPUS/corpus_manifest.json" "$RUN/corpus_manifest.json"

cat <<BANNER
--------------------------------------------------------------------
benchmark_mode=$MODE
measured_documents=$N
langgraph_mode=$LG_MODE   (bounded closed-loop HTTP window)
langgraph_client_window=$LG_CLIENT_WINDOW
langgraph_server_executor=default (~min(32, cpu_count+4) workers, INERT config)
rocketride_mode=$RR_MODE  (blast = one whole-corpus SDK batch; b<B> = consecutive SDK batches of B on one connection; c<N>/seq = closed-loop)
rocketride_threads_requested=${RR_THREADS:-unset (engine default)}
rocketride_arm_cpus=$ARM_CPUS
omp_num_threads=1 (pinned on BOTH arms)
NOTE: threads requested != threads activated != effective cores.
--------------------------------------------------------------------
BANNER
say "building both arms + bench client"
BUILD_ARMS=""
[ "${SKIP_LG:-0}" != "1" ] && BUILD_ARMS="langgraph"
[ "${SKIP_RR:-0}" != "1" ] && BUILD_ARMS="$BUILD_ARMS rocketride"
[ -n "$BUILD_ARMS" ] || { echo "FATAL: SKIP_LG=1 and SKIP_RR=1 leaves no arm to run"; exit 1; }
docker compose build --build-arg RR_DUP_PATCH="$RR_DUP_PATCH" \
  --build-arg RR_LENSORT_PATCH="${RR_LENSORT_PATCH:-0}" \
  $BUILD_ARMS 2>&1 | tail -12
docker compose --profile client build bench 2>&1 | tail -3
for i in $BUILD_ARMS; do
  docker image inspect "bench-$i:latest" --format "$i {{.Id}} {{.Architecture}}" \
    >> "$RUN/image_ids.txt"
done
say "$(tr '\n' ' ' < "$RUN/image_ids.txt")"

wait_healthy() {  # $1 container, $2 service
  local s
  for _ in $(seq 1 60); do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || echo none)
    [ "$s" = healthy ] && return 0
    if docker logs "$1" 2>&1 | grep -qi "Failed to compile constraints\|Python error 1"; then
      say "BOOT FAILED for $1:"; docker logs --tail 30 "$1"; return 1
    fi
    sleep 5
  done
  docker compose logs --tail 40 "$2"; return 1
}

# ------------------------------------------------------------------ LG arm
# SKIP_LG=1 runs the RocketRide arm alone — for engine-tuning probes (thread
# sweeps, scheduler experiments) where a comparison arm adds nothing. The
# report handles the absent arm; the run is a PROBE, not a comparison, and
# cross-arm/determinism verdicts will reflect that.
if [ "${SKIP_LG:-0}" != "1" ]; then
say "=== LangGraph — $REPS x $N docs, mode=$LG_MODE ==="
[ "$LG_EXTRACTOR" = tika ] && docker compose up -d tika
docker compose up -d langgraph
wait_healthy "$LG" langgraph || { echo FATAL; exit 1; }
docker exec "$LG" python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/meta',timeout=10).read().decode())" \
  > "$RUN/meta_lg.json"
docker run --rm --entrypoint pip bench-langgraph:latest freeze > "$RUN/pip_freeze_lg.txt"

for r in $(seq 1 "$REPS"); do
  D="$RUN/lg/rep$r"; mkdir -p "$D"
  say "lg rep$r/$REPS"
  docker exec -e SAMPLE_MAX_S="${SAMPLE_MAX_S:-86400}" -i "$LG" python3 - < bench/cgroup_sampler.py \
    > "$D/sampler.jsonl" 2>"$D/sampler.err" &
  S1=$!
  if [ "$LG_EXTRACTOR" = tika ]; then
    docker exec -e SAMPLE_MAX_S="${SAMPLE_MAX_S:-86400}" -i bench-tika python3 - < bench/cgroup_sampler.py \
      > "$D/sampler_tika.jsonl" 2>/dev/null &
    S2=$!
  else S2=""; fi
  sleep 2
  docker compose --profile client run --rm bench \
    /bench/lg_driver.py /corpus "/results/$REL/lg/rep$r" "$N" "$LG_MODE" "$WARM"
  sleep 2
  kill $S1 ${S2:-} 2>/dev/null || true; wait $S1 ${S2:-} 2>/dev/null || true
  sleep 10   # let the service settle so rep N+1 does not inherit rep N's tail
done
docker compose stop langgraph tika   # tika must NOT idle on the
                                     # arm cores during RR
else
  say "SKIP_LG=1 — LangGraph arm skipped (RocketRide-only probe run)"
fi

# ------------------------------------------------------------------ RR arm
# SKIP_RR=1 runs the LangGraph arm alone -- the mirror of SKIP_LG, for adding
# one arm to a campaign whose other arm has already been measured. The run is
# then a single-arm PROBE and the report's cross-arm section will say so.
if [ "${SKIP_RR:-0}" != "1" ]; then
say "=== RocketRide — $REPS x $N docs, mode=$RR_MODE ==="
docker compose up -d rocketride
wait_healthy "$RR" rocketride || { echo FATAL; exit 1; }
docker exec "$RR" sh -c 'sha256sum /opt/rocketride/engine/engine' > "$RUN/engine_sha.txt"
docker logs "$RR" > "$RUN/engine_boot.log" 2>&1

for r in $(seq 1 "$REPS"); do
  D="$RUN/rr/rep$r"; mkdir -p "$D"
  say "rr rep$r/$REPS"
  docker exec -e SAMPLE_MAX_S="${SAMPLE_MAX_S:-86400}" -i "$RR" python3 - < bench/cgroup_sampler.py \
    > "$D/sampler.jsonl" 2>"$D/sampler.err" &
  S3=$!
  sleep 2
  set +e
  docker compose --profile client run --rm bench \
    /bench/rr_driver.py /corpus "/results/$REL/rr/rep$r" "$N" "$RR_MODE" "$WARM"
  DRV=$?
  set -e
  sleep 2
  kill "$S3" 2>/dev/null || true; wait "$S3" 2>/dev/null || true
  [ "$DRV" -eq 0 ] || say "WARNING: rr driver rc=$DRV — rep$r records are suspect"
  sleep 10
done
docker logs --tail 300 "$RR" > "$RUN/engine_run.log" 2>&1 || true
docker compose stop rocketride 2>/dev/null || true
else
  say "SKIP_RR=1 — RocketRide arm skipped (LangGraph-only probe run)"
fi

# --------------------------------------------------------------- provenance
python3 run/write_provenance.py "$RUN"

say "deriving metrics"
set +e
python3 bench/report.py "$RUN" | tee "$RUN/report.txt"
RC=${PIPESTATUS[0]}
set -e

# The S3 prefix carries the run tag when one was given (a dated, named prefix
# is findable; a bare stamp is not), else the stamp as before.
S3_DEST="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench}/${RUN_TAG:-$STAMP}/"
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  aws s3 cp "$RUN/" "$S3_DEST" --recursive --only-show-errors \
    && say "exfil OK -> $S3_DEST" || say "WARNING: S3 upload failed"
else
  say "WARNING: no aws cli/role (run/install_awscli.sh); results only on box disk"
fi
say "results in $RUN"
[ "$RC" -eq 0 ] && say "RUN PASS" || say "RUN FAIL — see report.txt; numbers kept, never quoted"
exit "$RC"
