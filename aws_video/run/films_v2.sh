#!/usr/bin/env bash
# ARCHIVE_FILMS_V2 BENCHMARK RUNNER, ON THE BOX: run a frozen nested
# subset (10 ⊂ 50 ⊂ 100 ⊂ 500, acceptance-order prefixes) through both
# arms. Hardened 2026-08-23 (pre-500 review):
#
#   preflight   manifest seal verified (sha256 vs sidecar vs pin header),
#               corpus name + doc count + per-doc fields checked, EVERY
#               pulled video hashed once against the manifest BEFORE either
#               arm, license classification recorded
#   timeouts    BENCH_TIMEOUT_S default 86400; RR_PIPE_TTL_S default
#               +7200 (validated > timeout here AND in the driver)
#   provenance  image digests, LG pip freeze + versions, model hashes
#               (best-effort), host + git state -> $OUT/provenance/
#   exit        any of rr/lg/report/final-sync nonzero -> nonzero, one
#               FINAL STATUS line with every code (report gates included)
#
#   SUBSET=10 (default) | 50 | 100 | 500   N defaults to SUBSET-2 (+2 warm)
#   RocketRide: blast, default threads     LangGraph: c32, one uvicorn
#   32 cores UNPINNED, single rep — evidence grade SIZING (see report).
#
#   nohup bash run/films_v2.sh > ~/logs/films10.log 2>&1 < /dev/null &
#   SUBSET=500 nohup bash run/films_v2.sh > ~/logs/films500.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SUBSET="${SUBSET:-10}"
SETFILE="corpus/sets/archive_films_${SUBSET}.txt"
[ -f "$SETFILE" ] || { echo "FATAL: no $SETFILE (frozen subsets are 10/50/100/500)" >&2; exit 1; }
N="${N:-$((SUBSET - 2))}"
WARM="${WARM:-2}"
RR_MODE="${RR_MODE:-blast}"
LG_MODE="${LG_MODE:-c32}"
# Partial reruns: ARMS="lg" PAIR_RR=results/<run>/rr reruns only LangGraph and
# reports it against an already-finished RocketRide directory (and vice
# versa with PAIR_LG). Preflight + hashing still run: the corpus is re-proven.
ARMS="${ARMS:-rr lg}"
PAIR_RR="${PAIR_RR:-}"
PAIR_LG="${PAIR_LG:-}"
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_films_v2_$SUBSET}"
S3_CORPUS="s3://rocketride-benchmark-data/leela/corpus/archive_films_v2"
OUT="results/films$SUBSET-$STAMP"
S3_DEST="s3://rocketride-benchmark-data/leela/videobench/films$SUBSET-$STAMP/"
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-86400}"
export RR_PIPE_TTL_S="${RR_PIPE_TTL_S:-$((BENCH_TIMEOUT_S + 7200))}"
[ "$RR_PIPE_TTL_S" -gt "$BENCH_TIMEOUT_S" ] || {
  echo "FATAL: RR_PIPE_TTL_S ($RR_PIPE_TTL_S) must exceed BENCH_TIMEOUT_S ($BENCH_TIMEOUT_S)" >&2
  exit 1
}
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
mkdir -p "$OUT/rr" "$OUT/lg" "$OUT/provenance" "$CORPUS_DIR"

# Keepalive: corpus pull + hashing are low-CPU — the exact profile the idle
# watchdog kills (bitten during AMI staging).
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

echo "== [1/7] preflight: manifest seal + pin verification (before any video byte)"
"$AWS_BIN" s3 cp "$S3_CORPUS/corpus_manifest.json" "$CORPUS_DIR/corpus_manifest.json" --quiet
"$AWS_BIN" s3 cp "$S3_CORPUS/corpus_manifest.sha256" "$OUT/provenance/corpus_manifest.sha256" --quiet
msha_actual=$(sha256sum "$CORPUS_DIR/corpus_manifest.json" | cut -d' ' -f1)
msha_sidecar=$(tr -d ' \n' < "$OUT/provenance/corpus_manifest.sha256")
msha_pin=$(head -1 "$SETFILE" | grep -o 'manifest sha [0-9a-f]*' | awk '{print $3}')
[ "$msha_actual" = "$msha_sidecar" ] || { echo "FATAL: manifest sha $msha_actual != sidecar $msha_sidecar" >&2; exit 1; }
[ "$msha_actual" = "$msha_pin" ] || { echo "FATAL: manifest sha $msha_actual != pin header $msha_pin" >&2; exit 1; }
echo "   seal verified: $msha_actual"
python3 - "$SETFILE" "$CORPUS_DIR/corpus_manifest.json" "$SUBSET" "$OUT/provenance/license_classification.json" <<'PY'
import json, re, sys
setfile, mpath, subset, licout = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
m = json.load(open(mpath))
assert m.get("corpus") == "archive_films_v2", f"wrong corpus: {m.get('corpus')}"
assert m.get("n_docs") == 500, f"wrong n_docs: {m.get('n_docs')}"
ids = [l.split("\t")[0] for l in open(setfile) if not l.startswith("#") and l.strip()]
assert len(ids) == subset, f"pin has {len(ids)} entries, expected {subset}"
assert len(set(ids)) == len(ids), "duplicate identifiers in pin"
missing = []
for i in ids:
    doc = f"{i}.mp4"
    if doc not in m.get("duration_s", {}): missing.append(f"{doc}:duration")
    if doc not in m.get("video_duration_s", {}): missing.append(f"{doc}:video_duration")
    sh = m.get("sha256", {}).get(doc, {})
    if not sh.get("sha256"): missing.append(f"{doc}:sha256")
    if not sh.get("bytes"): missing.append(f"{doc}:bytes")
assert not missing, f"manifest incomplete for subset: {missing[:6]}"
# License classification (exact families; NC/ND kept-and-classified for
# sizing runs per 2026-08-23 policy decision — never silently dropped).
fams = {}
review = []
for i in ids:
    doc = f"{i}.mp4"
    u = re.sub(r"^https?://(www\.)?", "", str(m.get("license", {}).get(doc, "")).lower()).rstrip("/")
    if "publicdomain/mark" in u: fam = "pd_mark"
    elif "publicdomain/zero" in u: fam = "cc0"
    elif "licenses/publicdomain" in u: fam = "pd_license"
    elif re.search(r"licenses/by-nc-nd", u): fam = "by-nc-nd"
    elif re.search(r"licenses/by-nc-sa", u): fam = "by-nc-sa"
    elif re.search(r"licenses/by-nd", u): fam = "by-nd"
    elif re.search(r"licenses/by-nc", u): fam = "by-nc"
    elif re.search(r"licenses/by-sa", u): fam = "by-sa"
    elif re.search(r"licenses/by", u): fam = "by"
    else: fam = "other_or_missing"
    fams[fam] = fams.get(fam, 0) + 1
    if fam in ("by-nc", "by-nd", "by-nc-sa", "by-nc-nd", "other_or_missing"):
        review.append({"doc": doc, "family": fam})
json.dump({"policy": "PD/CC0/BY/BY-SA freely usable; NC/ND kept-and-"
                     "classified (internal sizing); review before publication",
           "counts": fams, "policy_review": review}, open(licout, "w"), indent=1)
print(f"   manifest fields complete for {subset} docs; licenses: {fams}"
      + (f"; {len(review)} flagged for pre-publication review" if review else ""))
PY

echo "== [2/7] corpus: archive_films_v2 subset $SUBSET from S3"
if [ "$SUBSET" = "500" ]; then
  "$AWS_BIN" s3 sync "$S3_CORPUS/" "$CORPUS_DIR/" --exclude "pins/*" --exclude "corpus_manifest.*" --quiet
else
  grep -v '^#' "$SETFILE" | while IFS=$'\t' read -r id _; do
    [ -f "$CORPUS_DIR/$id.mp4" ] || "$AWS_BIN" s3 cp "$S3_CORPUS/$id.mp4" "$CORPUS_DIR/" --quiet
  done
fi
have=$(find "$CORPUS_DIR" -name '*.mp4' | wc -l | tr -d ' ')
[ "$have" -eq "$SUBSET" ] || { echo "FATAL: pulled $have/$SUBSET videos" >&2; exit 1; }
echo "   $have videos, $(du -sh "$CORPUS_DIR" | cut -f1)"

echo "== [3/7] hash verification: every file once, BEFORE either arm"
( cd "$CORPUS_DIR" && ls *.mp4 | xargs -P 8 -n 1 sha256sum ) > "$OUT/preflight_hashes.txt"
python3 - "$CORPUS_DIR/corpus_manifest.json" "$OUT/preflight_hashes.txt" "$CORPUS_DIR" "$OUT/preflight_hashes.json" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1]))
shas = m["sha256"]
verified, bad = {}, []
for line in open(sys.argv[2]):
    h, name = line.split()
    want = shas.get(name, {})
    nbytes = os.path.getsize(os.path.join(sys.argv[3], name))
    if want.get("sha256") != h:
        bad.append(f"{name}: sha {h[:12]} != pinned {str(want.get('sha256'))[:12]}")
    elif want.get("bytes") != nbytes:
        bad.append(f"{name}: {nbytes} bytes != pinned {want.get('bytes')}")
    else:
        verified[name] = {"sha256": h, "bytes": nbytes}
if bad:
    sys.exit("PREFLIGHT HASH FAIL: " + "; ".join(bad[:5]))
json.dump({"verified": verified, "n": len(verified),
           "basis": "hashed once pre-run; drivers and gates reuse"},
          open(sys.argv[4], "w"))
print(f"   {len(verified)} files verified against the frozen manifest")
PY

echo "== [4/7] build images + provenance capture"
docker compose build rocketride langgraph smoke
PROV="$OUT/provenance"
{ git rev-parse HEAD; git describe --always --dirty 2>/dev/null || true; } > "$PROV/git_state.txt" 2>/dev/null || true
{ uname -a; nproc; lscpu 2>/dev/null | head -12; } > "$PROV/host.txt" || true
for img in $(docker compose config --images 2>/dev/null); do
  docker image inspect "$img" --format '{{.RepoTags}} id={{.Id}} digests={{.RepoDigests}} created={{.Created}}' \
    >> "$PROV/images.txt" 2>/dev/null || true
done
docker compose run --rm --no-deps --entrypoint pip langgraph freeze > "$PROV/lg_pip_freeze.txt" 2>/dev/null || echo "capture_failed" > "$PROV/lg_pip_freeze.txt"
docker compose run --rm --no-deps --entrypoint python langgraph -V > "$PROV/lg_python_version.txt" 2>/dev/null || true
docker compose run --rm --no-deps --entrypoint python langgraph -c \
  'import imageio_ffmpeg as f; print(f.get_ffmpeg_version())' > "$PROV/lg_ffmpeg_version.txt" 2>/dev/null || true
timeout 300 docker compose run --rm --no-deps --entrypoint sh langgraph -c \
  'find / -xdev \( -name "*.safetensors" -o -name "*.pth" -o -name "*.onnx" \) 2>/dev/null | head -20 | xargs -r sha256sum' \
  > "$PROV/lg_model_hashes.txt" 2>/dev/null || echo "best_effort_none_found (runtime-downloaded at service start)" >> "$PROV/lg_model_hashes.txt"
sha256sum arms/rocketride/Dockerfile > "$PROV/rr_dockerfile.sha256" || true
cp "$SETFILE" "$PROV/" || true
{ echo "BENCH_TIMEOUT_S=$BENCH_TIMEOUT_S"; echo "RR_PIPE_TTL_S=$RR_PIPE_TTL_S";
  echo "SUBSET=$SUBSET N=$N WARM=$WARM RR_MODE=$RR_MODE LG_MODE=$LG_MODE"; } > "$PROV/run_config.txt"
echo "   provenance -> $PROV/"

( while true; do "$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet 2>/dev/null || true; sleep 60; done ) &
SYNC_PID=$!
trap 'kill $SYNC_PID $KEEPALIVE_PID 2>/dev/null || true' EXIT

rc_rr=0; rc_lg=0
if echo " $ARMS " | grep -q " rr "; then
echo "== [5/7] ARM 1: RocketRide ($RR_MODE, $N docs + $WARM warm, unpinned)"
docker compose up -d rocketride
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-rocketride 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 60 ] && { echo "FATAL: RR engine never healthy"; exit 1; }
  sleep 5
done
RR_SAMPLER=$(sampler videobench-rocketride "$OUT/rr/engine_cgroup.csv")
rc_rr=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/bench_video.py /corpus "/results/films$SUBSET-$STAMP/rr" "$N" "$RR_MODE" "$WARM" \
  > "$OUT/rr/driver.log" 2>&1 || rc_rr=$?
kill "$RR_SAMPLER" 2>/dev/null || true
docker compose logs --no-color rocketride > "$OUT/rr/service.log" 2>&1 || true
docker compose stop rocketride && docker compose rm -f rocketride
echo "   RR done (rc=$rc_rr)"
else
echo "== [5/7] ARM 1: RocketRide SKIPPED (ARMS=$ARMS; pairing with ${PAIR_RR:-nothing})"
fi

if echo " $ARMS " | grep -q " lg "; then
echo "== [6/7] ARM 2: LangGraph ($LG_MODE, $N docs + $WARM warm, unpinned)"
docker compose up -d langgraph
for i in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' videobench-langgraph 2>/dev/null)" = "healthy" ] && break
  [ "$i" = 90 ] && { echo "FATAL: LG service never healthy"; exit 1; }
  sleep 5
done
LG_SAMPLER=$(sampler videobench-langgraph "$OUT/lg/engine_cgroup.csv")
rc_lg=0
CORPUS="$CORPUS_DIR" docker compose run --rm smoke \
  python /bench/lg_driver.py /corpus "/results/films$SUBSET-$STAMP/lg" "$N" "$LG_MODE" "$WARM" \
  > "$OUT/lg/driver.log" 2>&1 || rc_lg=$?
kill "$LG_SAMPLER" 2>/dev/null || true
docker compose logs --no-color langgraph > "$OUT/lg/service.log" 2>&1 || true
docker compose down
echo "   LG done (rc=$rc_lg)"
else
echo "== [6/7] ARM 2: LangGraph SKIPPED (ARMS=$ARMS; pairing with ${PAIR_LG:-nothing})"
fi

echo "== [7/7] report + final sync"
RR_DIR="$OUT/rr"; LG_DIR="$OUT/lg"
[ -n "$PAIR_RR" ] && { RR_DIR="$PAIR_RR"; cp -r "$PAIR_RR" "$OUT/rr_paired"; }
[ -n "$PAIR_LG" ] && { LG_DIR="$PAIR_LG"; cp -r "$PAIR_LG" "$OUT/lg_paired"; }
rc_rep=0
python3 bench/report.py --arms "$RR_DIR" "$LG_DIR" > "$OUT/report.txt" 2>&1 || rc_rep=$?
cat "$OUT/report.txt"
kill $SYNC_PID 2>/dev/null || true
rc_sync=0
"$AWS_BIN" s3 sync "$OUT" "$S3_DEST" --quiet || rc_sync=$?
[ "$rc_sync" -eq 0 ] && echo "uploaded: $S3_DEST"

# One FINAL STATUS line; ANY nonzero component fails the wrapper — a report
# with failed hard gates must never exit 0 (exit-semantics review 2026-08-23).
echo "== FINAL STATUS films$SUBSET-$STAMP: rc_rr=$rc_rr rc_lg=$rc_lg rc_report=$rc_rep rc_sync=$rc_sync"
overall=0
for rc in "$rc_rr" "$rc_lg" "$rc_rep" "$rc_sync"; do
  [ "$rc" -ne 0 ] && overall=1
done
if [ "$overall" -eq 0 ]; then
  echo "   execution PASS + validity PASS (evidence grade: see report verdict — single rep = SIZING)"
else
  echo "   FAILURE — inspect the nonzero component above; report: $OUT/report.txt"
fi
exit "$overall"
