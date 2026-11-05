#!/usr/bin/env bash
# Corpus: the first N govdocs PDFs, fetched ON THE BOX (there is no scp path).
#
# The corpus directory is NAMED FOR N -- ~/bench_corpus_<N>. N therefore cannot
# drift between fetch and run: a different N is a different directory, so a
# stale 50-doc corpus can never be silently reused for a 200-doc run, and
# re-fetching the same N is a no-op instead of a clobber.
#
# Selection is the first N sorted BY BASENAME across however many archives it
# took. Sorting by basename rather than archive order keeps document identity
# stable: bench_corpus_200 is always the same 200 documents.
#
# Extraction uses python3's zipfile, not unzip -- the box has neither unzip nor
# sudo to install it (preflight flags this), and python3 is required anyway.
#
# OFFSET skips the first OFFSET PDFs, so a later run can use a document set
# DISJOINT from an earlier one -- results carried over from a corpus the code
# has already been tuned against are not independent evidence.
#
# EXTRA documents beyond N are fetched for warm-up, so warm-up documents are
# never also measured documents (a measured doc that was warmed is cache-hot
# and no longer comparable to its peers).
#
#   bash corpus/fetch_govdocs.sh [n] [dest_dir] [offset] [extra]
#   bash corpus/fetch_govdocs.sh 200 "" 200 5     # docs 201-400, +5 warm
set -euo pipefail
N="${1:-200}"
OFFSET="${3:-0}"
EXTRA="${4:-0}"
TOTAL=$(( N + EXTRA ))
DEST="${2:-$HOME/bench_corpus_n${N}_off${OFFSET}}"
[ -n "${2:-}" ] || true
BASE="https://digitalcorpora.s3.amazonaws.com/corpora/files/govdocs1/zipfiles"
MAX_ARCHIVES="${MAX_ARCHIVES:-10}"

# Idempotent: an existing, complete, verified corpus is left alone.
if [ -f "$DEST/corpus_manifest.json" ] && [ -f "$DEST/SHA256SUMS" ]; then
  have=$(find "$DEST" -name '*.pdf' | wc -l | tr -d ' ')
  if [ "$have" -eq "$TOTAL" ] && (cd "$DEST" && sha256sum -c --quiet SHA256SUMS 2>/dev/null); then
    echo "corpus already present and verified: $DEST ($N measured + $EXTRA warm)"
    exit 0
  fi
  echo "existing corpus at $DEST is incomplete or failed verification — rebuilding"
fi

mkdir -p "$DEST"
rm -f "$DEST"/*.pdf "$DEST/SHA256SUMS" "$DEST/corpus_manifest.json"

CACHES=()
for i in $(seq 0 $((MAX_ARCHIVES - 1))); do
  idx=$(printf '%03d' "$i")
  CACHE="$HOME/govdocs_${idx}.zip"
  if [ ! -s "$CACHE" ]; then
    echo "downloading govdocs ${idx}.zip (cached in \$HOME after first use)"
    curl -fsSL --max-time 1800 "$BASE/${idx}.zip" -o "$CACHE"
  fi
  CACHES+=("$CACHE")
  have=$(python3 - "${CACHES[@]}" <<'PYX'
import sys, zipfile, pathlib
seen = set()
for c in sys.argv[1:]:
    with zipfile.ZipFile(c) as z:
        seen.update(pathlib.PurePosixPath(i.filename).name for i in z.infolist()
                    if i.filename.lower().endswith(".pdf") and not i.is_dir())
print(len(seen))
PYX
)
  echo "  archives: ${#CACHES[@]}  PDFs available: $have  (need $(( OFFSET + TOTAL )))"
  [ "$have" -ge "$(( OFFSET + TOTAL ))" ] && break
done

python3 - "$DEST" "$TOTAL" "$OFFSET" "$N" "${CACHES[@]}" <<'PY'
import json, pathlib, sys, zipfile
dest, total, offset, measured = (pathlib.Path(sys.argv[1]), int(sys.argv[2]),
                                 int(sys.argv[3]), int(sys.argv[4]))
caches = sys.argv[5:]
found = {}
for c in caches:
    with zipfile.ZipFile(c) as z:
        for i in z.infolist():
            if i.filename.lower().endswith(".pdf") and not i.is_dir():
                found.setdefault(pathlib.PurePosixPath(i.filename).name, (c, i))
need = offset + total
if len(found) < need:
    sys.exit(f"FATAL: only {len(found)} PDFs across {len(caches)} archives, "
             f"need {need} (offset {offset} + {total}). Raise MAX_ARCHIVES.")
# Sorted by basename, then sliced -- so a given (offset, total) always yields
# the same documents no matter how many archives it took.
picked = sorted(found)[offset:offset + total]
for name in picked:
    c, info = found[name]
    with zipfile.ZipFile(c) as z:
        (dest / name).write_bytes(z.read(info))
(dest / "corpus_manifest.json").write_text(json.dumps({
    "corpus": "govdocs1",
    "source": "digitalcorpora.s3.amazonaws.com/corpora/files/govdocs1/zipfiles",
    "selection_rule": "first N PDFs sorted by basename across archives consumed",
    "n_measured": measured,
    "n_warm_extra": total - measured,
    "offset": offset,
    "archives": [pathlib.Path(c).name for c in caches],
    "docs_measured": picked[:measured],
    "docs_warm": picked[measured:],
}, indent=1))
print(f"extracted {total} PDFs (offset {offset}: {measured} measured "
      f"+ {total-measured} warm) from {len(caches)} archive(s)")
PY

cd "$DEST"
sha256sum *.pdf > SHA256SUMS
sha256sum -c --quiet SHA256SUMS
echo
echo "corpus ready: $DEST"
echo "  docs      : $(ls -1 ./*.pdf | wc -l | tr -d ' ')"
echo "  manifest  : $DEST/corpus_manifest.json"
echo "  sha256sums: $DEST/SHA256SUMS (verified)"
