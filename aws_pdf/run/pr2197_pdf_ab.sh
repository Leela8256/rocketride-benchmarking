#!/usr/bin/env bash
# A/B of rocketride-server PR #2197 (length-sorted embedding batching) on the
# PDF pipeline. RocketRide only, DEFAULT posture, 1 rep per cell.
#
#   cell A  stock 3.3.1        RR_LENSORT_PATCH=0   arrival-order batching
#   cell B  3.3.1 + PR #2197   RR_LENSORT_PATCH=1   length-sorted batching
#
# Why PDFs and not video: the patch removes padding waste, which only exists
# when chunks in a batch differ in length AND a document spans more than one
# batch (encode() default batch_size=32). Our video corpus had ~85 chunks per
# document, every one of them 4000 chars of detection JSON, all exceeding
# MiniLM's 512-token ceiling and therefore truncated to exactly 512 — uniform,
# nothing to sort, measured +0.08%. GovDocs1 is different: chunks/doc runs
# 1..276 (p50 6, p90 36), mean chunk length 7..3983 chars, and 57.6% of all
# chunks live in documents big enough to span multiple batches.
set -uo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
N="${N:-1000}"
REPS="${REPS:-1}"

cell () {   # $1 label, $2 RR_LENSORT_PATCH
  echo
  echo "############ PDF PR2197 CELL $1  (RR_LENSORT_PATCH=$2, default posture, N=$N, REPS=$REPS)"
  RR_LENSORT_PATCH="$2" docker compose build \
    --build-arg RR_LENSORT_PATCH="$2" rocketride 2>&1 | tail -3
  echo "   in-image verification:"
  docker run --rm --entrypoint sh bench-rocketride:latest -c \
    'F=$(find /opt/rocketride -path "*/ai/common/models/transformers/sentence_transformers.py" | head -1); \
     if grep -q "order = sorted(range(len(sentences))" "$F"; then echo "     LENGTH-SORTED batching present"; \
     else echo "     arrival-order batching (stock)"; fi' 2>&1 | tail -1

  RUN_TAG="pr2197pdf-$1-$STAMP" SKIP_LG=1 N="$N" REPS="$REPS" \
  RR_LENSORT_PATCH="$2" \
    bash run/matched_run.sh < /dev/null || echo "!!! CELL $1 FAILED (rc=$?) — continuing"
}

cell stock   0
cell patched 1

echo
echo "=== PDF PR2197 A/B SUMMARY $STAMP ==="
python3 - "$STAMP" <<'PY'
import glob, json, os, re, sys
st = sys.argv[1]
rows = {}
for d in sorted(glob.glob("results/pr2197pdf-*-%s" % st)):
    cell = os.path.basename(d).split("-")[1]
    txt = ""
    for f in ("report.txt",):
        p = os.path.join(d, f)
        if os.path.exists(p):
            txt = open(p, errors="ignore").read()
    g = {}
    for m in re.finditer(r"([\w_]+)\s*[:=]\s*([0-9]+\.?[0-9]*)", txt):
        k, v = m.group(1), float(m.group(2))
        if k in ("docs_per_s","chunks_per_s","cpu_s_per_doc","cpu_s_per_chunk",
                 "effective_cores","span_s","wall_s") and k not in g:
            g[k] = v
    rows[cell] = g
    print(f"\n--- {cell} ---")
    for k in ("docs_per_s","chunks_per_s","cpu_s_per_doc","cpu_s_per_chunk","effective_cores","span_s","wall_s"):
        if k in g: print(f"    {k:18s} {g[k]}")
a, b = rows.get("stock", {}), rows.get("patched", {})
for k, label in (("docs_per_s","throughput (docs/s)"), ("chunks_per_s","chunks/s"),
                 ("cpu_s_per_chunk","CPU per chunk  <- the stage the patch touches")):
    if a.get(k) and b.get(k):
        d = (b[k]/a[k]-1)*100
        sign = "" if k.startswith("cpu") else ""
        print(f"\n{label}: {d:+.2f}% (patched vs stock)")
print("\n1 rep per cell. RocketRide default posture is ~0.12% pass-to-pass, so a")
print("delta above ~0.5% is worth a 3-rep confirmation; below that, treat as flat.")
PY

touch "results/pr2197pdf-CAMPAIGN-DONE-${STAMP}"
echo "=== CAMPAIGN COMPLETE ${STAMP} ==="
