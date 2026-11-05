#!/usr/bin/env bash
# ONE-TIME full-corpus staging, ON THE BOX — DATA_FLOW_PLAN.md §2, v2.
#
# Unified per-meeting loop over corpus/sets/ami_full.txt (170 meetings):
#   source:  S3 if the muxed video is already staged anywhere we know
#            (ami_full itself, then ami30h, then ami30test — server-fast),
#            else the AMI mirror (download pair + bitexact mux)
#   probe:   VIDEO-stream duration via ffmpeg null-mux (fixes the
#            frame_law corpus finding: ~10% of AMI meetings have A/V
#            stream-length mismatch, so audio-derived durations are
#            wrong for frame arithmetic)
#   record:  sha256 + bytes + video_duration_s for EVERY file (uniform —
#            no carve-outs for pre-seeded videos)
#   upload:  to $S3_CORPUS if not already there; local copy deleted
#            (delete-as-you-go: ~400 MB peak disk)
#   manifest: duration_s (audio, from the EDA — the footage denominator)
#            + video_duration_s + sha256 maps → S3
#
# RESUMABLE: probe/sha state lives in $SHAS; S3 listing is upload state.
#
#   nohup bash run/stage_corpus.sh > ~/logs/stage.log 2>&1 < /dev/null &
set -euo pipefail
cd "$(dirname "$0")/.."

LIST=corpus/sets/ami_full.txt
DURS=corpus/sets/ami_full_durations.json
S3_CORPUS="${S3_CORPUS:-s3://rocketride-benchmark-data/leela/corpus/ami_full}"
SEEDS="s3://rocketride-benchmark-data/leela/corpus/ami30h s3://rocketride-benchmark-data/leela/corpus/ami30test"
BASE="https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus"
CAM="Closeup1"
WORK="$HOME/stage_tmp"
SHAS="$HOME/stage_shas.jsonl"
AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"
[ -x "$AWS_BIN" ] || AWS_BIN="$HOME/.local/bin/aws"
FFMPEG="$(command -v ffmpeg || echo "$HOME/bin/ffmpeg")"
[ -x "$FFMPEG" ] || { echo "FATAL: no ffmpeg (fetch_ami.sh installs the static build)" >&2; exit 1; }
mkdir -p "$WORK"; touch "$SHAS"

vdur() {  # video-stream duration in seconds, via null stream-copy
  "$FFMPEG" -nostdin -i "$1" -map 0:v:0 -c copy -f null - 2>&1 | \
    grep -oE 'time=[0-9:.]+' | tail -1 | cut -d= -f2 | \
    python3 -c 'import sys; h,m,s=sys.stdin.read().strip().split(":"); print(round(int(h)*3600+int(m)*60+float(s),2))'
}

echo "== inventory"
"$AWS_BIN" s3 ls "$S3_CORPUS/" 2>/dev/null | awk '/\.avi$/{print $NF}' > "$WORK/.in_full" || true
for seed in $SEEDS; do
  tag=$(basename "$seed")
  "$AWS_BIN" s3 ls "$seed/" 2>/dev/null | awk '/\.avi$/{print $NF}' > "$WORK/.in_$tag" || true
done
total=$(grep -cv '^#' "$LIST")
echo "   already in ami_full: $(wc -l < "$WORK/.in_full" | tr -d ' ')/$total"

n=0
grep -v '^#' "$LIST" | while read -r m; do
  n=$((n+1))
  f="$m.avi"
  # done = uploaded AND probed
  if grep -qx "$f" "$WORK/.in_full" && grep -q "\"$f\"" "$SHAS"; then
    continue
  fi
  mux="$WORK/$f"
  src="mirror"
  # cheapest available source for the bytes
  if grep -qx "$f" "$WORK/.in_full"; then
    "$AWS_BIN" s3 cp "$S3_CORPUS/$f" "$mux" --quiet; src="ami_full"
  else
    for seed in $SEEDS; do
      tag=$(basename "$seed")
      if grep -qx "$f" "$WORK/.in_$tag" 2>/dev/null; then
        "$AWS_BIN" s3 cp "$seed/$f" "$mux" --quiet; src="$tag"; break
      fi
    done
  fi
  if [ "$src" = "mirror" ]; then
    vid="$WORK/$m.$CAM.avi"; wav="$WORK/$m.Mix-Headset.wav"
    curl -fsSL --max-time 1800 "$BASE/$m/video/$m.$CAM.avi" -o "$vid"
    curl -fsSL --max-time 1800 "$BASE/$m/audio/$m.Mix-Headset.wav" -o "$wav"
    "$FFMPEG" -nostdin -loglevel error -y -i "$vid" -i "$wav" \
      -map 0:v:0 -map 1:a:0 -c copy \
      -map_metadata -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
      "$mux"
    rm -f "$vid" "$wav"
  fi
  vd=$(vdur "$mux")
  sha=$(sha256sum "$mux" | cut -d' ' -f1)
  if ! grep -qx "$f" "$WORK/.in_full"; then
    "$AWS_BIN" s3 cp "$mux" "$S3_CORPUS/$f" --quiet
  fi
  # one uniform record per file; last write wins on resume
  grep -v "\"$f\"" "$SHAS" > "$SHAS.tmp" || true; mv "$SHAS.tmp" "$SHAS"
  echo "{\"doc\": \"$f\", \"sha256\": \"$sha\", \"bytes\": $(stat -c%s "$mux"), \"video_duration_s\": $vd}" >> "$SHAS"
  rm -f "$mux"
  echo "staged $f ($n/$total, src=$src, vdur=${vd}s)"
done

echo "== manifest"
python3 - "$DURS" "$SHAS" <<'PY' > "$WORK/corpus_manifest.json"
import json, sys
durs = json.load(open(sys.argv[1]))
shas, vdurs = {}, {}
for line in open(sys.argv[2]):
    r = json.loads(line)
    shas[r["doc"]] = {"sha256": r["sha256"], "bytes": r["bytes"]}
    vdurs[r["doc"]] = r["video_duration_s"]
print(json.dumps({
    "corpus": "ami_full",
    "source": "groups.inf.ed.ac.uk/ami/AMICorpusMirror (mux: Closeup1 + Mix-Headset, bitexact)",
    "n_docs": len(durs),
    "selection_rule": "all usable AMI meetings sorted by ID (TS3003d excluded: no Closeup1)",
    "duration_s": durs,
    "video_duration_s": vdurs,
    "total_hours": round(sum(durs.values()) / 3600, 2),
    "sha256": shas,
    "note": "duration_s = audio (the footage denominator); video_duration_s = "
            "ffmpeg-probed video stream (the frame_law denominator — ~10% of "
            "AMI meetings have A/V stream-length mismatch)",
}, indent=1))
PY
"$AWS_BIN" s3 cp "$WORK/corpus_manifest.json" "$S3_CORPUS/corpus_manifest.json" --quiet

final=$("$AWS_BIN" s3 ls "$S3_CORPUS/" | grep -c '\.avi$')
probed=$(wc -l < "$SHAS" | tr -d ' ')
echo "corpus staged: $final/$total videos in S3, $probed probed+sha'd, manifest uploaded"
rm -rf "$WORK"
[ "$final" -eq "$total" ] && [ "$probed" -eq "$total" ]
