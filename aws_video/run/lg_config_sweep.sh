#!/usr/bin/env bash
# LangGraph config sweep — find the arm's best posture on this box before
# spending a full-corpus run on it.
#
# Detection is 92-98% of the work, so the question is: how many RF-DETR
# forward passes per second can 32 cores do? Three axes, one control:
#   processes W   — splits the GIL for the Python-side work (json.dumps per
#                   frame, chunking, embed calls); each costs a model copy
#   per-endpoint C — videos in flight per process (client concurrency = W x C)
#   torch threads  — intra-op width; 1 expected to win (sub-linear scaling)
#
# Subset, not the full corpus: N videos from the head of the committed AMI pin.
# Every cell is one arm only, fresh container, same corpus, same order.
#
#   bash run/lg_config_sweep.sh
#
# Env: N (32), SWEEP_TAG (sweep), CELLS (override the grid)
set -uo pipefail
cd "$(dirname "$0")/.."

N="${N:-96}"   # must exceed the largest concurrency (c48) or a cell is one wave
TAG="${SWEEP_TAG:-lgsweep}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# name              W  perEP torch arm    (client concurrency = W x perEP)
CELLS="${CELLS:-
lg_1proc_c32_t1     1  32    1     lgip
lg_2proc_c32_t1     2  16    1     lg
lg_4proc_c32_t1     4  8     1     lg
lg_8proc_c32_t1     8  4     1     lg
lg_4proc_c48_t1     4  12    1     lg
lg_1proc_c32_t2     1  32    2     lgip
}"

echo "=== LangGraph config sweep — $N videos/cell, stamp $STAMP"
echo "$CELLS" | grep -v '^[[:space:]]*$' | while read -r name W PER T ARM; do
  [ -n "${name:-}" ] || continue
  echo
  echo "############ CELL $name : W=$W per-endpoint=$PER c=$((W*PER)) torch=$T arm=$ARM"
  RUN_NAME="${TAG}-${name}-${STAMP}" \
  N="$N" ARM_ORDER="$ARM" \
  LG_WORKERS="$W" LG_PER_ENDPOINT_CONCURRENCY="$PER" \
  LG_EXPECT_DETECT_CONC="$PER" LG_TORCH_THREADS="$T" \
  LG_THREAD_LIMIT=64 \
  bash run/matched8x4_ami.sh < /dev/null || echo "!!! CELL $name FAILED (rc=$?) — continuing"
done

echo
echo "=== SWEEP SUMMARY $STAMP"
python3 - "$TAG" "$STAMP" <<'PY'
import glob, json, os, re, sys
tag, stamp = sys.argv[1], sys.argv[2]
rows = []
for d in sorted(glob.glob(f"results/{tag}-*-{stamp}")):
    cell = os.path.basename(d)[len(tag) + 1:-(len(stamp) + 1)]
    rep = os.path.join(d, "report.txt")
    got = {}
    if os.path.exists(rep):
        for line in open(rep):
            m = re.match(r"\s*([\w ]+):\s*(\{.*\})\s*$", line)
            if not m:
                continue
            try:
                b = json.loads(m.group(2))
            except Exception:
                continue
            for k in ("frames_per_s", "x_realtime", "span_s", "effective_cores",
                      "cpu_s_per_frame", "peak_memory_gb", "idle_burden_cores"):
                if k in b and k not in got:
                    got[k] = b[k]
    rows.append((cell, got))
hdr = ["cell", "frames_per_s", "x_realtime", "effective_cores", "cpu_s_per_frame", "span_s"]
print(" | ".join(f"{h:<20}" if h == "cell" else f"{h:>16}" for h in hdr))
for cell, g in rows:
    print(" | ".join([f"{cell:<20}"] + [f"{str(g.get(h, '-')):>16}" for h in hdr[1:]]))
best = max((r for r in rows if isinstance(r[1].get("frames_per_s"), (int, float))),
           key=lambda r: r[1]["frames_per_s"], default=None)
print(f"\nbest by frames/s: {best[0]} = {best[1]['frames_per_s']}" if best else "\nno cell produced a throughput number")
PY
