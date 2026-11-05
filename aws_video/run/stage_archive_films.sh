#!/usr/bin/env bash
# ONE-TIME archive_films corpus staging, ON THE BOX — the stage_corpus.sh
# (v2, AMI) pattern with the AMI mirror swapped for archive.org. Same
# discipline end to end:
#
#   list:    corpus/sets/archive_films.txt (pinned by census_archive_films.py:
#            identifier<TAB>filename) — the corpus contract
#   source:  S3 if already staged (resume is server-fast), else
#            https://archive.org/download/<id>/<file>  (direct HTTP, no auth;
#            302 to a datanode; ~5-20 MB/s/stream — be polite, stay sequential)
#   gate:    ffprobe MUST find an audio stream (dual-lane pipe needs speech;
#            silent films that slipped the census are SKIPPED and logged)
#   probe:   VIDEO-stream duration via ffmpeg null-mux (frame_law denominator)
#   record:  sha256 + bytes + video_duration_s for EVERY file (corpus_pin)
#   upload:  <identifier>.mp4 -> $S3_CORPUS; local copy deleted per film
#            (delete-as-you-go: peak disk = one film, <4 GB)
#   manifest: duration_s (census) + video_duration_s + sha256 maps -> S3
#
# RESUMABLE: probe/sha state in $SHAS; S3 listing is upload state. The idle
# watchdog WILL stop the box during this network-bound work — harmless,
# relaunch and it continues. ~300 GB for 500 films ≈ an overnight run.
#
#   nohup bash run/stage_archive_films.sh > ~/logs/stage_films.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

LIST=corpus/sets/archive_films.txt
DURS=corpus/sets/archive_films_durations.json
S3_CORPUS="${S3_CORPUS:-s3://rocketride-benchmark-data/leela/corpus/archive_films}"
WORK="$HOME/stage_films_tmp"
SHAS="$HOME/stage_films_shas.jsonl"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
FFMPEG="$(command -v ffmpeg || echo "$HOME/bin/ffmpeg")"
FFPROBE="$(command -v ffprobe || echo "$HOME/bin/ffprobe")"
[ -x "$FFMPEG" ] || { echo "FATAL: no ffmpeg (fetch_ami.sh installs the static build)" >&2; exit 1; }
mkdir -p "$WORK"; touch "$SHAS"

# Keepalive: the idle watchdog stops the box during network-bound staging
# (bitten during AMI staging). One busy core reads as "not idle".
( while :; do :; done ) &
KEEPALIVE_PID=$!
trap 'kill $KEEPALIVE_PID 2>/dev/null || true' EXIT

vprobe() {  # "duration_s frames fps" via one null stream-copy pass
  "$FFMPEG" -nostdin -i "$1" -map 0:v:0 -c copy -f null - 2>&1 | python3 -c '
import re, sys
t = sys.stdin.read()
m = re.findall(r"time=(\d+):(\d+):([\d.]+)", t)
h, mi, se = m[-1] if m else ("0","0","0")
dur = round(int(h)*3600 + int(mi)*60 + float(se), 2)
fr = re.findall(r"frame=\s*(\d+)", t)
fps = re.search(r"([\d.]+) fps", t)
print(dur, fr[-1] if fr else 0, fps.group(1) if fps else 0)'
}

echo "== inventory"
"$AWS_BIN" s3 ls "$S3_CORPUS/" 2>/dev/null | awk '/\.mp4$/{print $NF}' > "$WORK/.in_s3" || true
total=$(grep -cv '^#' "$LIST")
echo "   already in S3: $(wc -l < "$WORK/.in_s3" | tr -d ' ')/$total"

n=0
grep -v '^#' "$LIST" | while IFS=$'\t' read -r id srcfile; do
  n=$((n+1))
  doc="$id.mp4"
  if grep -qx "$doc" "$WORK/.in_s3" && grep -q "\"$doc\"" "$SHAS"; then
    continue
  fi
  local_f="$WORK/$doc"
  if grep -qx "$doc" "$WORK/.in_s3"; then
    "$AWS_BIN" s3 cp "$S3_CORPUS/$doc" "$local_f" --quiet; src="s3"
  else
    # archive.org rate-limits aggressive clients; -fL follows the datanode 302
    curl -fsSL --retry 3 --retry-delay 10 --max-time 7200 \
      "https://archive.org/download/$id/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$srcfile")" \
      -o "$local_f"; src="archive"
  fi
  # audio gate: dual-lane needs a transcript lane; skip silent prints.
  # ffprobe when present; else ffmpeg stderr (the box's static bundle
  # ships only ffmpeg).
  if [ -x "$FFPROBE" ]; then
    has_audio=$("$FFPROBE" -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$local_f" 2>/dev/null | grep -c . || true)
  else
    has_audio=$("$FFMPEG" -nostdin -i "$local_f" 2>&1 | grep -c "Audio:" || true)
  fi
  if [ "$has_audio" -eq 0 ]; then
    echo "SKIP $doc ($n/$total): no audio stream"
    grep -v "\"$doc\"" "$SHAS" > "$SHAS.tmp" || true; mv "$SHAS.tmp" "$SHAS"
    echo "{\"doc\": \"$doc\", \"skipped\": \"no_audio\"}" >> "$SHAS"
    rm -f "$local_f"
    continue
  fi
  read -r vd vframes vfps <<< "$(vprobe "$local_f")"
  sha=$(sha256sum "$local_f" | cut -d' ' -f1)
  if ! grep -qx "$doc" "$WORK/.in_s3"; then
    "$AWS_BIN" s3 cp "$local_f" "$S3_CORPUS/$doc" --quiet
  fi
  grep -v "\"$doc\"" "$SHAS" > "$SHAS.tmp" || true; mv "$SHAS.tmp" "$SHAS"
  echo "{\"doc\": \"$doc\", \"sha256\": \"$sha\", \"bytes\": $(stat -c%s "$local_f"), \"video_duration_s\": $vd, \"frames_counted\": $vframes, \"nominal_fps\": $vfps}" >> "$SHAS"
  rm -f "$local_f"
  echo "staged $doc ($n/$total, src=$src, vdur=${vd}s)"
done

echo "== manifest"
python3 - "$DURS" "$SHAS" <<'PY' > "$WORK/corpus_manifest.json"
import json, sys
durs = json.load(open(sys.argv[1]))
shas, vdurs, skipped, fpsmap = {}, {}, [], {}
for line in open(sys.argv[2]):
    r = json.loads(line)
    if r.get("skipped"):
        skipped.append(r["doc"]); continue
    shas[r["doc"]] = {"sha256": r["sha256"], "bytes": r["bytes"]}
    vdurs[r["doc"]] = r["video_duration_s"]
    if r.get("frames_counted"):
        fpsmap[r["doc"]] = {"frames_counted": r["frames_counted"],
                            "nominal_fps": r["nominal_fps"]}
staged = {d: s for d, s in durs.items() if d in shas}
print(json.dumps({
    "corpus": "archive_films",
    "source": "archive.org collection feature_films (h.264/MPEG4 derivatives, "
              "selection pinned by corpus/census_archive_films.py)",
    "n_docs": len(staged),
    "selection_rule": "top-downloads of eligible pool: 60-240 min, mp4, "
                      "non-silent (census + staging ffprobe audio gate), deduped titles",
    "skipped_no_audio": skipped,
    "duration_s": staged,
    "video_duration_s": vdurs,
    "video_fps_probe": fpsmap,
    "total_hours": round(sum(staged.values()) / 3600, 2),
    "sha256": shas,
    "note": "duration_s = census metadata (footage denominator); "
            "video_duration_s = ffmpeg-probed video stream (frame_law denominator)",
}, indent=1))
PY
"$AWS_BIN" s3 cp "$WORK/corpus_manifest.json" "$S3_CORPUS/corpus_manifest.json" --quiet

final=$("$AWS_BIN" s3 ls "$S3_CORPUS/" | grep -c '\.mp4$')
probed=$(grep -c '"sha256"' "$SHAS" || true)
skips=$(grep -c '"skipped"' "$SHAS" || true)
echo "corpus staged: $final videos in S3, $probed probed+sha'd, $skips skipped(no audio), manifest uploaded"
rm -rf "$WORK"
