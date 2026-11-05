#!/usr/bin/env bash
# NODE-LEVEL ATTRIBUTION on the AMI VIDEO corpus, BOTH ARMS — the video-track
# twin of aws_pdf/run/nodeprofile.sh.
#
# The question: where does each arm's per-video time actually go, node by node,
# on the same videos, in the posture a user gets without tuning anything?
#
#   cell rr-traced   RocketRide, N videos, blast, RR_TRACE=summary
#                    -> the engine's own apaevt_flow enter/leave events, one
#                       pair per component, written to flow_events.jsonl.
#   cell rr-control  RocketRide, the SAME videos, same mode, RR_TRACE UNSET
#                    -> the perturbation control (see below). Nothing else
#                       differs between this cell and rr-traced.
#   cell lg          LangGraph, the same videos, c32
#                    -> per-node timings are already recorded on EVERY video
#                       (lg_timings: frames_s / detect_s / chunk_s / embed_s,
#                       arms/langgraph/graph.py), so LangGraph needs no tracing
#                       switch and has no perturbation to control for. Its
#                       assemble stage is NOT timed — an asymmetry the
#                       comparison states rather than hides.
#   cell rr-seq      RocketRide, 6 hand-picked videos ONE AT A TIME, traced
#                    -> per-VIDEO node attribution. apaevt_flow carries no file
#                       identity, so the only way to say WHICH video a node
#                       spent its time on is to have exactly one video in
#                       flight: in seq mode every flow event falls inside one
#                       video's [submit_ns, completion_ns] window on the same
#                       clock. Blast cannot do this, at any trace level.
#
# WHY THE CONTROL IS NOT OPTIONAL
#   Tracing rides the same websocket the driver measures on, and the engine
#   does extra work to emit it. If tracing moves throughput, the traced cell
#   describes a DIFFERENT run from the one we are trying to explain. So
#   rr-traced is compared against rr-control on x_realtime, frames_per_s and
#   cpu_s_per_frame (plus effective_cores for context), and any delta over 5%
#   is flagged: past that the node shares are indicative only and must never
#   be quoted as the untraced baseline's breakdown.
#
# POSTURE DISCLOSURE — BOTH ARMS UNTUNED, exactly as every prior AMI run
#   RocketRide  ONE pipeline, no `threads` argument (RR_THREADS unset), the six
#               BLAS/OMP vars UNSET (the plain `rocketride` service, never
#               `rocketride-matched`), blast ingestion, UNPINNED.
#   LangGraph   ONE uvicorn process on 8200 (the plain `langgraph` service),
#               c32 client concurrency, UNPINNED.
#   No cpuset envelope on either arm; one arm at a time; single rep. This is a
#   DEFAULT-POSTURE ATTRIBUTION STUDY. It is not the matched campaign and its
#   cross-arm numbers are not a matched comparison. The keepalive busy loop
#   (below) burns ~1 core on the box for the campaign's whole lifetime and is
#   part of the disclosed conditions, as in every campaign in this repo.
#
# NESTING CAVEAT — READ THIS BEFORE QUOTING ANY NODE NUMBER
#   RocketRide drives the whole downstream chain synchronously inside the
#   upstream node's lane call, so apaevt_flow spans NEST: on the detect pipe
#   frame_grabber_1 contains detect_1 contains preprocessor_1 contains
#   embedding_1 contains response_1. A raw leave-minus-enter total is therefore
#   INCLUSIVE and always indicts the outermost node — the one node that is
#   certainly not the problem.
#   >>> NEVER quote a raw leave-minus-enter number. In node_timings.json that
#   >>> is the `inclusive_total_s` column; it is kept only so the trap stays
#   >>> visible in the artifact.
#   bench/flow_selftime.py subtracts the nested children and reports EXCLUSIVE
#   SELF TIME; `total_s` and `pct_of_node_time` are the quotable figures, and
#   bench_video.py already routes node_timings.json through the same function.
#   `pct_of_span` is NOT a fraction of the run: with C items in flight the node
#   totals sum to roughly C x span and it can exceed 100.
#   Prefer the flow_selftime.py CLI output over node_timings.json: it adds the
#   LangGraph join and, for seq runs, the per-video table.
#
# SEQ-CELL COVERAGE — a known, disclosed limit of the cross-arm join
#   The bulk cells measure the FIRST N of ami_full in sorted order (N=24 ->
#   EN2001a..ES2003d). The seq six are chosen for ROOM-TYPE DIVERSITY, and four
#   of them (IN1007, IN1016, ES2016d, TS3009c) are nowhere near the first 24.
#   flow_selftime.py restricts a seq-mode --lg join to documents ok in BOTH
#   runs, so `rr-seq --lg lg` covers only EN2001a and ES2002a. The seq cell's
#   own per-video RocketRide table is complete and unaffected. To get all six
#   on both arms add the optional cell: CELLS="... lg-seq" (LangGraph seq over
#   the same curated corpus); it is OFF by default because it is not one of the
#   four decided cells.
#
# COST at the defaults (N=24 -> 19.57 h of footage, 24 measured + 2 warm):
#   rr-traced ~33 min, rr-control ~33 min (RR measured 36x realtime on blast),
#   lg ~8 min (LG measured 150x), rr-seq: 4.62 h of footage but ONE video at a
#   time, which exercises far less of a 32-core box than blast — 4.62h/36x is
#   a floor (~8 min), not an estimate. Plus image builds on a cold box.
#
#   nohup bash run/nodeprofile_ami.sh > ~/logs/nodeprofile_ami.log 2>&1 </dev/null &
#   CELLS="rr-seq" bash run/nodeprofile_ami.sh     # one cell only
#   N=6 bash run/nodeprofile_ami.sh                # quicker shakedown
#   DRY=1 bash run/nodeprofile_ami.sh              # print every command, run none
#
# set -e is deliberately NOT used: a cell that fails must not abort the others.
set -uo pipefail
cd "$(dirname "$0")/.."

DATE="${DATE:-$(date -u +%Y-%m-%d)}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
CELLS="${CELLS:-rr-traced rr-control lg rr-seq}"
DRY="${DRY:-0}"

# ---- bulk cells (rr-traced / rr-control / lg), run through native170.sh ----
N="${N:-24}"                       # measured videos, first N of ami_full sorted
WARM_N="${WARM_N:-2}"              # warm videos, the list tail after N
RR_MODE="${RR_MODE:-blast}"        # RocketRide native path: ONE send_files batch
LG_MODE="${LG_MODE:-c32}"          # LangGraph default posture: c32 client conc.
                                   # (with N=24 < 32 the whole set is offered at
                                   # t=0; shot_meta still records
                                   # offered_concurrency=32 — disclose it so)
CORPUS_DIR="${CORPUS_DIR:-$HOME/bench_corpus_ami_full}"

# ---- seq cell: a curated 6, chosen for ROOM-TYPE DIVERSITY, not for speed ----
#   IN1007   40.19 min   294 chunks   dense Idiap room
#   IN1016   60.07 min   319 chunks   dense Idiap room (densest in the corpus)
#   ES2002a  21.21 min                sparse Edinburgh room
#   ES2016d  25.39 min                sparse Edinburgh room
#   EN2001a  87.50 min                the long one
#   TS3009c  43.00 min                TS room
# = 4.62 h of footage. The two warm videos MUST sort AFTER every measured name:
# the drivers take the measured set as sorted(dir)[:n] and the warm set as the
# tail sorted(dir)[n:n+warm] (bench/bench_video.py:190-192, :344). TS3010a and
# TS3012a are the shortest names that sort after TS3009c.
SEQ_VIDEOS="${SEQ_VIDEOS:-IN1007 IN1016 ES2002a ES2016d EN2001a TS3009c}"
SEQ_WARM="${SEQ_WARM:-TS3010a TS3012a}"          # 17.35 / 14.32 min
SEQ_CORPUS_DIR="${SEQ_CORPUS_DIR:-$HOME/bench_corpus_ami_nodeseq}"
SEQ_MODE="${SEQ_MODE:-seq}"        # bench_video.py supports blast | seq | c<N>
                                   # (bench_video.py:382-387) — seq is native.

S3_CORPUS="${S3_CORPUS:-s3://rocketride-benchmark-data/leela/corpus/ami_full}"
S3_PREFIX="${S3_PREFIX:-s3://rocketride-benchmark-data/leela/videobench/nodeprofile-ami-$DATE}"

# The detect-only pipe, as every AMI run since 2026-08-21. Compose defaults to
# the DUAL-LANE benchmark_video.pipe, which is NOT what this campaign measures;
# native170.sh:35 exports the same value for its own cells.
export BENCH_PIPE=/pipe/benchmark_video_detect.pipe
export BENCH_TIMEOUT_S="${BENCH_TIMEOUT_S:-21600}"
# Untuned RocketRide: no worker-pool request, no client-side socket cap, no
# per-connection admission cap.
unset RR_THREADS RR_POOL_MAX RR_THREADS_PER_CONN

ANALYSIS="results/nodeprofile-ami-analysis-$STAMP"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"

say()   { echo "[nodeprofile-ami $(date -u +%H:%M:%S)] $*"; }
has_cell() { case " $CELLS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
# "would this exist?" — a real artifact, or a selected cell under DRY, so the
# dry run prints the sequence for the cells ASKED FOR and not for the others.
planned() { [ -f "$1" ] || { [ "$DRY" = "1" ] && has_cell "$2"; }; }
runsh() {                        # one command line; DRY=1 prints instead of runs
  if [ "$DRY" = "1" ]; then echo "    \$ $*"; return 0; fi
  eval "$@"
}

# ---- keepalive: the box's idle watchdog stops it during long image builds and
# during the seq cell, which leaves most cores idle. Last core only, started
# before any cell, killed on exit.
NCPU="$( (command -v nproc >/dev/null && nproc) || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2 )"
if [ "$DRY" = "1" ]; then
  echo "    \$ taskset -c \$(( \$(nproc) - 1 )) bash -c 'while :; do :; done' &   # keepalive, trap-killed"
else
  taskset -c "$(( NCPU - 1 ))" bash -c 'while :; do :; done' &
  KEEP=$!
  trap 'kill $KEEP 2>/dev/null' EXIT
fi

say "campaign $STAMP  cells=[$CELLS]  N=$N warm=$WARM_N  rr=$RR_MODE lg=$LG_MODE"
say "  pipe=$BENCH_PIPE  corpus=$CORPUS_DIR  S3=$S3_PREFIX/"
say "  DRY=$DRY  keepalive on core $(( NCPU - 1 ))"
[ "$DRY" = "1" ] || mkdir -p "$ANALYSIS"

# ---------------------------------------------------------------- bulk cells
# native170.sh is the default-posture runner and already does everything a bulk
# cell needs: pulls ami_full from S3, builds only the images the arm needs
# (NATIVE_ARMS), brings the engine up and waits for health, samples the engine
# cgroup every 15 s, runs the driver in the smoke container, collects service
# logs, writes report.txt and syncs to S3. It takes N / WARM / RR_MODE /
# LG_MODE / CORPUS_DIR / RUN_NAME / NATIVE_ARMS from the environment.
#
# RR_TRACE reaches the driver by inheritance only: `docker compose run` passes
# this shell's environment to compose, and compose maps RR_TRACE into the smoke
# container exactly as it maps RR_THREADS (docker-compose.yml, smoke service).
# Exporting it here IS the whole plumbing — no argument, no file edit.
bulk_cell() {                    # $1 cell name, $2 NATIVE_ARMS
  local cell="$1" arms="$2" rn rc
  rn="nodeprofile-ami-${cell}-${STAMP}"
  echo
  say "=============== cell $cell  (arms=$arms, run=$rn) ==============="
  if [ "$cell" = "rr-traced" ]; then
    export RR_TRACE=summary
    say "  RR_TRACE=summary  -> apaevt_flow enter/leave per component"
  else
    unset RR_TRACE
    say "  RR_TRACE unset    -> tracing off$([ "$cell" = rr-control ] && echo ' (this is the control)')"
  fi
  runsh "RUN_NAME=\"$rn\" NATIVE_ARMS=\"$arms\" N=\"$N\" WARM=\"$WARM_N\" \
RR_MODE=\"$RR_MODE\" LG_MODE=\"$LG_MODE\" CORPUS_DIR=\"$CORPUS_DIR\" \
bash run/native170.sh < /dev/null"
  rc=$?
  unset RR_TRACE
  say "  cell $cell finished rc=$rc (report.txt grading a single rep as SIZING is expected)"
}

# ----------------------------------------------------------------- seq cells
# Written inline rather than through native170.sh: native170.sh binds the whole
# staged ami_full corpus and takes the measured set as the first N sorted, so it
# cannot express "these six by name". Every other step mirrors it exactly (same
# build, same health wait, same 15 s cgroup sampler, same compose run, same log
# collection), so the seq records are shaped like the bulk ones.
sampler() {                      # $1 container, $2 csv  (verbatim from native170.sh)
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

# Hardlink the curated six + two warm out of the staged ami_full: no bytes are
# copied, and the sha256 in corpus_manifest.json still describes the file.
seq_corpus() {
  local m
  say "  curated corpus -> $SEQ_CORPUS_DIR"
  runsh "mkdir -p \"$SEQ_CORPUS_DIR\""
  for m in $SEQ_VIDEOS $SEQ_WARM; do
    runsh "[ -f \"$CORPUS_DIR/$m.avi\" ] || \"$AWS_BIN\" s3 cp \"$S3_CORPUS/$m.avi\" \"$CORPUS_DIR/$m.avi\" --only-show-errors"
    runsh "ln -f \"$CORPUS_DIR/$m.avi\" \"$SEQ_CORPUS_DIR/$m.avi\" 2>/dev/null || cp -f \"$CORPUS_DIR/$m.avi\" \"$SEQ_CORPUS_DIR/$m.avi\""
  done
  # The manifest carries duration_s / video_duration_s / sha256 for all 170; the
  # drivers only look up the names present in the dir, so copying it whole is
  # correct. Without it x_realtime is null and the corpus_pin gate SKIPs.
  runsh "cp -f \"$CORPUS_DIR/corpus_manifest.json\" \"$SEQ_CORPUS_DIR/corpus_manifest.json\" || echo '   !!! no corpus_manifest.json — durations and the corpus_pin gate will be missing'"
}

# Provenance + the one check that can silently ruin the cell: the warm videos
# must sort AFTER every measured name, or the driver measures the wrong six.
seq_selection() {                # $1 out file
  if [ "$DRY" = "1" ]; then
    echo "    \$ python3 <seq-selection-check> \"$SEQ_CORPUS_DIR\" \"$1\" \"$SEQ_VIDEOS\" \"$SEQ_WARM\""
    return 0
  fi
  mkdir -p "$(dirname "$1")"
  python3 - "$SEQ_CORPUS_DIR" "$1" "$SEQ_VIDEOS" "$SEQ_WARM" <<'PY'
import json, os, sys
d, outf, meas, warm = sys.argv[1], sys.argv[2], sys.argv[3].split(), sys.argv[4].split()
mf = os.path.join(d, "corpus_manifest.json")
man = json.load(open(mf)) if os.path.exists(mf) else {}
vd, src = man.get("video_duration_s", {}), man.get("duration_s", {})
present = sorted(f for f in os.listdir(d) if f.endswith(".avi"))
dur = lambda m: vd.get(m + ".avi") or src.get(m + ".avi") or 0
sel = {
    "corpus": "ami_full (curated subset)",
    "selection_rule": "room-type diversity: 2 dense Idiap (IN), 2 sparse "
                      "Edinburgh (ES), 1 long EN, 1 TS — NOT chosen for speed",
    "mode": "seq (one video at a time, RR_TRACE=summary)",
    "measured": meas, "warm": warm, "present_sorted": present,
    "sorted_prefix_is_measured": present[:len(meas)] == sorted(m + ".avi" for m in meas),
    "video_duration_s": {m + ".avi": dur(m) for m in meas + warm},
    "measured_footage_s": round(sum(dur(m) for m in meas), 1),
}
open(outf, "w").write(json.dumps(sel, indent=1))
print("   present: %d avi; measured footage %.2f h" % (len(present), sel["measured_footage_s"] / 3600))
for m in meas:
    print("     %-10s %7.1f s = %5.1f min" % (m, dur(m), dur(m) / 60))
print("   sorted-prefix check: %s" % sel["sorted_prefix_is_measured"])
if not sel["sorted_prefix_is_measured"]:
    print("   !!! the warm videos do NOT sort last — the driver would measure the WRONG SIX")
    print("   !!! fix SEQ_WARM (names must sort after every SEQ_VIDEOS name) before trusting this cell")
PY
}

seq_cell() {                     # $1 arm: rr | lg
  local arm="$1" rn out n_meas n_warm rc svc cont drv mode
  n_meas=$(echo $SEQ_VIDEOS | wc -w | tr -d ' ')
  n_warm=$(echo $SEQ_WARM   | wc -w | tr -d ' ')
  if [ "$arm" = "rr" ]; then
    rn="nodeprofile-ami-rr-seq-${STAMP}"; svc=rocketride; cont=videobench-rocketride
    drv=bench_video.py; mode="$SEQ_MODE"
  else
    rn="nodeprofile-ami-lg-seq-${STAMP}"; svc=langgraph;  cont=videobench-langgraph
    drv=lg_driver.py;  mode=seq
  fi
  out="results/$rn"
  echo
  say "=============== cell ${arm}-seq  (run=$rn) ==============="
  say "  measured ($n_meas, one at a time): $SEQ_VIDEOS"
  say "  warm     ($n_warm, sort after every measured name): $SEQ_WARM"

  say "  [1/6] curated corpus"
  runsh "mkdir -p \"$out/$arm\""
  seq_corpus
  seq_selection "$out/$arm/seq_selection.json"

  say "  [2/6] build images ($svc + smoke)"
  runsh "docker compose build $svc smoke"

  say "  [3/6] $svc up, wait healthy"
  runsh "docker compose up -d $svc"
  runsh "for i in \$(seq 1 90); do \
[ \"\$(docker inspect -f '{{.State.Health.Status}}' $cont 2>/dev/null)\" = healthy ] && break; \
[ \"\$i\" = 90 ] && echo '   FATAL: $svc never healthy'; sleep 5; done"

  say "  [4/6] sampler + $drv mode=$mode$([ "$arm" = rr ] && echo ' RR_TRACE=summary')"
  local SEQ_SAMPLER=""
  if [ "$DRY" = "1" ]; then
    echo "    \$ SEQ_SAMPLER=\$(sampler $cont \"$out/$arm/engine_cgroup.csv\")"
  else
    SEQ_SAMPLER=$(sampler "$cont" "$out/$arm/engine_cgroup.csv")
  fi
  if [ "$arm" = "rr" ]; then
    runsh "RR_TRACE=summary CORPUS=\"$SEQ_CORPUS_DIR\" docker compose run --rm smoke \
python /bench/$drv /corpus \"/results/$rn/$arm\" $n_meas $mode $n_warm \
> \"$out/$arm/driver.log\" 2>&1"
  else
    runsh "CORPUS=\"$SEQ_CORPUS_DIR\" docker compose run --rm smoke \
python /bench/$drv /corpus \"/results/$rn/$arm\" $n_meas $mode $n_warm \
> \"$out/$arm/driver.log\" 2>&1"
  fi
  rc=$?
  if [ "$DRY" = "1" ]; then echo "    \$ kill \$SEQ_SAMPLER"; else kill "$SEQ_SAMPLER" 2>/dev/null || true; fi

  say "  [5/6] logs + teardown"
  runsh "docker compose logs --no-color $svc > \"$out/$arm/service.log\" 2>&1 || true"
  runsh "docker compose stop $svc && docker compose rm -f $svc"

  say "  [6/6] report (single arm, so the seq cell carries its own gates)"
  runsh "python3 bench/report.py \"$out/$arm\" > \"$out/report.txt\" 2>&1; cat \"$out/report.txt\""
  say "  cell ${arm}-seq finished rc=$rc"
}

# ------------------------------------------------------------------ dispatch
for CELL in $CELLS; do
  case "$CELL" in
    rr-traced)  bulk_cell rr-traced rr ;;
    rr-control) bulk_cell rr-control rr ;;
    lg)         bulk_cell lg lg ;;
    rr-seq)     seq_cell rr ;;
    lg-seq)     seq_cell lg ;;          # optional, off by default — see header
    *)          say "!!! unknown cell '$CELL' — skipped" ;;
  esac
done
unset RR_TRACE

TRACED="results/nodeprofile-ami-rr-traced-$STAMP/rr"
CONTROL="results/nodeprofile-ami-rr-control-$STAMP/rr"
LG="results/nodeprofile-ami-lg-$STAMP/lg"
SEQ="results/nodeprofile-ami-rr-seq-$STAMP/rr"
LGSEQ="results/nodeprofile-ami-lg-seq-$STAMP/lg"

# ---------------------------------------------------------------- analysis
# bench/flow_selftime.py contract: positional RR rep dir holding
# flow_events.jsonl + per_doc.jsonl; --lg an LG rep dir holding per_doc.jsonl
# with lg_timings; --json <path> optional machine-readable dump. Its stdout IS
# the quotable exclusive-self-time table, captured verbatim so this runner
# never restates its numbers in its own words.
echo
say "=== ANALYSIS: exclusive self time (bench/flow_selftime.py) ==="
if [ ! -f bench/flow_selftime.py ] && [ "$DRY" != "1" ]; then
  say "!!! bench/flow_selftime.py is missing — node attribution cannot be computed."
  say "    Raw flow_events.jsonl / node_timings.json remain in the cell dirs."
fi
LGARG=""
if planned "$LG/per_doc.jsonl" lg; then LGARG=" --lg \"$LG\""; fi
for pair in "traced:$TRACED:rr-traced" "seq:$SEQ:rr-seq"; do
  tag="${pair%%:*}"; rest="${pair#*:}"; dir="${rest%%:*}"; owner="${rest#*:}"
  if planned "$dir/flow_events.jsonl" "$owner"; then
    say "  flow_selftime: $tag"
    runsh "python3 bench/flow_selftime.py \"$dir\"$LGARG \
--json \"$ANALYSIS/selftime_$tag.json\" > \"$ANALYSIS/selftime_$tag.txt\" 2>&1; \
cat \"$ANALYSIS/selftime_$tag.txt\""
  else
    say "  flow_selftime: $tag SKIPPED (no $dir/flow_events.jsonl — tracing produced nothing)"
  fi
done
# Only if the optional lg-seq cell ran: the same six on both arms, so the
# per-video cross-arm table is not restricted to the 2 documents the bulk LG
# cell happens to share with the curated set.
if planned "$LGSEQ/per_doc.jsonl" lg-seq && planned "$SEQ/flow_events.jsonl" rr-seq; then
  say "  flow_selftime: seq vs lg-seq (all six on both arms)"
  runsh "python3 bench/flow_selftime.py \"$SEQ\" --lg \"$LGSEQ\" \
--json \"$ANALYSIS/selftime_seq_vs_lgseq.json\" > \"$ANALYSIS/selftime_seq_vs_lgseq.txt\" 2>&1; \
cat \"$ANALYSIS/selftime_seq_vs_lgseq.txt\""
fi

echo
say "=== ANALYSIS: paired report, traced vs lg ==="
if planned "$TRACED/per_doc.jsonl" rr-traced && planned "$LG/per_doc.jsonl" lg; then
  runsh "python3 bench/report.py --arms \"$TRACED\" \"$LG\" \
> \"$ANALYSIS/report_traced_vs_lg.txt\" 2>&1; cat \"$ANALYSIS/report_traced_vs_lg.txt\""
else
  say "  SKIPPED (need both $TRACED and $LG)"
fi

# ---------------------------------------------------------------- S3 upload
# native170.sh already syncs each bulk cell to s3://.../videobench/<RUN_NAME>/;
# this second copy gathers the whole campaign (seq cell and analysis included)
# under one dated prefix so it can be fetched as a unit.
echo
say "=== UPLOAD -> $S3_PREFIX/ ==="
for cell in $CELLS; do
  d="results/nodeprofile-ami-$cell-$STAMP"
  if [ -d "$d" ] || [ "$DRY" = "1" ]; then
    runsh "\"$AWS_BIN\" s3 cp \"$d\" \"$S3_PREFIX/nodeprofile-ami-$cell-$STAMP/\" \
--recursive --only-show-errors && echo \"   uploaded $d\""
  fi
done
runsh "\"$AWS_BIN\" s3 cp \"$ANALYSIS\" \"$S3_PREFIX/nodeprofile-ami-analysis-$STAMP/\" \
--recursive --only-show-errors && echo \"   uploaded $ANALYSIS\""

# ------------------------------------------------------------------ summary
echo
say "=== SUMMARY $STAMP ==="
if [ "$DRY" = "1" ]; then
  echo "    \$ python3 <summary> $STAMP   # cell table + perturbation table + node comparison"
else
python3 - "$STAMP" <<'PY'
import json, os, sys
sys.path.insert(0, "bench")
from report import load_run                      # noqa: E402
from metrics import v_metrics as vm              # noqa: E402

stamp = sys.argv[1]
CELLS = [("rr-traced", "rr"), ("rr-control", "rr"), ("lg", "lg"),
         ("rr-seq", "rr"), ("lg-seq", "lg")]


def block(cell, arm):
    d = f"results/nodeprofile-ami-{cell}-{stamp}/{arm}"
    if not os.path.exists(os.path.join(d, "per_doc.jsonl")):
        return None
    r = load_run(d)
    s = r["meta"].get("measurement_start_epoch_ns")
    e = r["meta"].get("measurement_end_epoch_ns")
    w = (s / 1e9, e / 1e9) if s and e else None
    cpu = vm.cpu_from_sampler(r["sampler"], w)
    return {"dir": d, "meta": r["meta"],
            "v1": vm.v1_throughput(r["records"], r["meta"]),
            "v3": vm.v3_efficiency(r["records"], r["meta"], cpu),
            "v4": vm.v4_resources(r["records"], r["meta"], cpu)}


B = {c: block(c, a) for c, a in CELLS}


def g(d, k):
    v = d.get(k)
    return "-" if v is None else v


print("\nCELLS")
print(f"  {'cell':<12}{'mode':>7}{'ok/n':>9}{'span s':>10}{'xRT':>9}"
      f"{'frames/s':>10}{'cpu s/fr':>10}{'cores':>8}  trace")
for c, _ in CELLS:
    b = B.get(c)
    if not b:
        print(f"  {c:<12}  (no per_doc.jsonl — cell not run, or it failed)")
        continue
    m, v1, v3 = b["meta"], b["v1"], b["v3"]
    print(f"  {c:<12}{m.get('mode', '?'):>7}"
          f"{str(m.get('ok_docs', '?')) + '/' + str(m.get('n_docs', '?')):>9}"
          f"{g(v1, 'span_s'):>10}{g(v1, 'x_realtime'):>9}{g(v1, 'frames_per_s'):>10}"
          f"{g(v3, 'cpu_s_per_frame'):>10}{g(v3, 'effective_cores'):>8}"
          f"  {m.get('trace_level') or 'off'}")

# ---- THE control check: does tracing change the run it is measuring? ----
t, c = B.get("rr-traced"), B.get("rr-control")
print("\nPERTURBATION CONTROL (rr-traced vs rr-control, >5% on any metric = flag)")
if not (t and c):
    print("  NOT AVAILABLE — both rr-traced and rr-control must have produced records.")
    print("  Without it the traced node shares cannot be attributed to the baseline;")
    print("  do not quote them as the untraced run's breakdown.")
else:
    perturbed = []
    for key, src in (("x_realtime", "v1"), ("frames_per_s", "v1"),
                     ("cpu_s_per_frame", "v3"), ("effective_cores", "v3")):
        a, b = c[src].get(key), t[src].get(key)
        if not a or not b:
            print(f"  {key:<17} control {str(a):>12}   traced {str(b):>12}"
                  f"          (not comparable)")
            continue
        d = 100 * (b / a - 1)
        flag = ""
        if abs(d) > 5:
            flag = "  <-- TRACING PERTURBS"
            perturbed.append(key)
        print(f"  {key:<17} control {a:>12.4f}   traced {b:>12.4f}   {d:+7.2f}%{flag}")
    if perturbed:
        print(f"  VERDICT: TRACING PERTURBS THE RUN ({', '.join(perturbed)} moved >5%).")
        print("  The traced cell's node shares are INDICATIVE ONLY and must not be")
        print("  quoted as the untraced baseline's breakdown.")
    else:
        print("  VERDICT: tracing is within 5% on every metric — the traced cell's node")
        print("  shares describe the same run as the untraced baseline.")

# ---- node comparison ----
print("\nNODE COMPARISON")
print("  RocketRide EXCLUSIVE self time per node: the flow_selftime tables printed")
print(f"  above, saved to results/nodeprofile-ami-analysis-{stamp}/selftime_*.txt")
print("  (+ .json). Those are the only RocketRide per-node numbers to quote, and")
print("  the seq table is the only one that attributes time to a NAMED video.")

lgb = B.get("lg")
if lgb and lgb["v4"].get("lg_stage_split"):
    v4 = lgb["v4"]
    print("\n  LangGraph per-node share (lg_timings summed over every video — recorded")
    print("  on every run, no tracing switch, so no perturbation to control for):")
    for k, v in v4["lg_stage_split"].items():
        print(f"    {k:<12}{v:>8}")
    print(f"    time outside the four timed nodes (assemble + framework): "
          f"{v4.get('lg_framework_overhead_s')} s")
else:
    print("\n  LangGraph: no lg_timings available (cell missing or failed).")

for cell in ("rr-traced", "rr-seq"):
    p = f"results/nodeprofile-ami-{cell}-{stamp}/rr/node_timings.json"
    if not os.path.exists(p):
        continue
    nt = json.load(open(p))
    nodes = nt.get("nodes") or {}
    print(f"\n  [{cell}] node_timings.json  (written by the driver through the SAME")
    print("  flow_selftime.summarize_flow — total_s is EXCLUSIVE self time)")
    print(f"    nested={nt.get('nested')} depth={nt.get('max_nesting_depth')} "
          f"unmatched_leave={nt.get('unmatched_leave')} "
          f"unclosed_enter={nt.get('unclosed_enter')} "
          f"warmup_dropped={nt.get('warmup_calls_dropped')}")
    if not nodes:
        print("    NO NODES: flow traces did not arrive. The trace level never reached")
        print("    the engine, or it emitted nothing — fall through to a truncated-pipe")
        print("    ablation rather than guessing.")
        continue
    print(f"    {'node':<22}{'calls':>8}{'self s':>10}{'% self':>8}"
          f"{'p95 ms':>10}{'INCLUSIVE s':>13}")
    for nm, v in list(nodes.items())[:12]:
        print(f"    {nm:<22}{v.get('calls', 0):>8}{v.get('total_s', 0):>10.1f}"
              f"{(v.get('pct_of_node_time') or 0):>8.1f}{(v.get('p95_ms') or 0):>10.1f}"
              f"{(v.get('inclusive_total_s') or 0):>13.1f}")
    print("    ^ the INCLUSIVE column is raw leave-minus-enter: it double-counts every")
    print("    nested child and always indicts the outermost node. NEVER quote it.")

print("\nPOSTURE: both arms untuned and unpinned, no cpuset envelope, single rep,")
print("keepalive busy loop on the last core throughout. Default-posture")
print("attribution study — not a matched comparison.")
PY
fi

runsh "touch \"results/nodeprofile-ami-CAMPAIGN-DONE-$STAMP\""
say "=== CAMPAIGN COMPLETE $STAMP ==="
