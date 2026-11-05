#!/usr/bin/env bash
# Corpus: the first N AMI scenario meetings, fetched ON THE BOX (no scp path).
#
# One benchmark document = ONE video file with sound. The AMI mirror does not
# ship that: the DivX .avi camera files carry a single video stream and NO
# audio track (verified against the RIFF header: 1x 'vids', 0x 'auds'), and
# audio is distributed separately as .wav. So each corpus doc is built by
# stream-copy muxing
#     <meeting>.Closeup1.avi  (video, DivX, no audio)
#   + <meeting>.Mix-Headset.wav (16 kHz mono PCM)
#   -> <meeting>.avi            (PCM-in-AVI, no re-encode, bitexact flags)
# Closeup1 exists in all three instrumented rooms (ES/IS/TS); the room-view
# camera names differ per site, so Closeup1 is the uniform choice.
#
# Selection is the first N meeting IDs, sorted, from the fixed scenario-meeting
# candidate list (ES2002-ES2016, IS1000-IS1009, TS3003-TS3012, sessions a-d),
# skipping IDs the mirror does not have (a few sessions were never recorded,
# e.g. IS1002a). Sorted-candidates + skip-missing is deterministic: a given
# (N, OFFSET) always yields the same meetings.
#
# The corpus directory is NAMED FOR (N, OFFSET) so a stale corpus can never be
# silently reused for a different run shape, and re-fetching is a no-op.
# OFFSET skips the first OFFSET meetings for a disjoint later set; EXTRA
# meetings beyond N are fetched for warm-up so warmed docs are never measured.
#
# Raw .avi/.wav downloads are cached in $HOME/ami_cache (like the govdocs zip
# cache), so rebuilding a corpus re-muxes instead of re-downloading ~100MB per
# meeting. ffmpeg: uses PATH if present, else ~/bin/ffmpeg, else downloads a
# static x86_64 build (the box has no sudo).
#
#   bash corpus/fetch_ami.sh [n] [dest_dir] [offset] [extra]
#   bash corpus/fetch_ami.sh 20 "" 0 2     # 20 measured + 2 warm meetings
#
# LIST MODE: MEETING_LIST=<file> overrides candidate enumeration with an
# explicit meeting set (one ID per line, # comments allowed) — how curated
# sets like corpus/sets/ami30h.txt are fetched. In list mode every listed
# meeting is fetched; N (arg 1) is how many count as measured (rest = warm),
# OFFSET/EXTRA are ignored, and a listed meeting missing from the mirror is
# FATAL rather than skipped: an explicit list has no room for silent holes.
#   MEETING_LIST=corpus/sets/ami30h.txt bash corpus/fetch_ami.sh 60
set -euo pipefail
N="${1:-20}"
OFFSET="${3:-0}"
EXTRA="${4:-0}"
MEETING_LIST="${MEETING_LIST:-}"
if [ -n "$MEETING_LIST" ]; then
  [ -f "$MEETING_LIST" ] || { echo "FATAL: MEETING_LIST=$MEETING_LIST not found" >&2; exit 1; }
  SETNAME="$(basename "$MEETING_LIST" .txt)"
  LISTED=($(grep -v '^#' "$MEETING_LIST" | tr -s ' \n' '\n' | grep .))
  TOTAL=${#LISTED[@]}
  EXTRA=$(( TOTAL - N ))
  OFFSET=0
  DEST="${2:-$HOME/bench_corpus_${SETNAME}}"
else
  TOTAL=$(( N + EXTRA ))
  DEST="${2:-$HOME/bench_corpus_ami_n${N}_off${OFFSET}}"
fi
BASE="https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus"
CAM="Closeup1"
CACHE="$HOME/ami_cache"

# Idempotent: an existing, complete, verified corpus is left alone.
if [ -f "$DEST/corpus_manifest.json" ] && [ -f "$DEST/SHA256SUMS" ]; then
  have=$(find "$DEST" -name '*.avi' | wc -l | tr -d ' ')
  if [ "$have" -eq "$TOTAL" ] && (cd "$DEST" && sha256sum -c --quiet SHA256SUMS 2>/dev/null); then
    echo "corpus already present and verified: $DEST ($N measured + $EXTRA warm)"
    exit 0
  fi
  echo "existing corpus at $DEST is incomplete or failed verification — rebuilding"
fi

# ffmpeg: PATH, then ~/bin, then a static build (no sudo on the box).
FFMPEG="$(command -v ffmpeg || true)"
if [ -z "$FFMPEG" ] && [ -x "$HOME/bin/ffmpeg" ]; then FFMPEG="$HOME/bin/ffmpeg"; fi
if [ -z "$FFMPEG" ]; then
  if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "FATAL: ffmpeg not found and static download is Linux-x86_64 only." >&2
    echo "Install ffmpeg on PATH and re-run." >&2
    exit 1
  fi
  echo "downloading static ffmpeg (no sudo available)"
  mkdir -p "$HOME/bin"
  curl -fsSL --max-time 900 \
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
    -o "$HOME/ffmpeg-static.tar.xz"
  tar -xJf "$HOME/ffmpeg-static.tar.xz" -C "$HOME" \
    --wildcards 'ffmpeg-*-amd64-static/ffmpeg'
  mv "$HOME"/ffmpeg-*-amd64-static/ffmpeg "$HOME/bin/ffmpeg"
  rm -rf "$HOME"/ffmpeg-*-amd64-static "$HOME/ffmpeg-static.tar.xz"
  FFMPEG="$HOME/bin/ffmpeg"
fi
"$FFMPEG" -version | head -1

if [ -n "$MEETING_LIST" ]; then
  # Explicit set: verify every meeting exists, fail on any hole.
  for m in "${LISTED[@]}"; do
    if ! curl -sIf "$BASE/$m/video/$m.$CAM.avi" >/dev/null 2>&1 \
    || ! curl -sIf "$BASE/$m/audio/$m.Mix-Headset.wav" >/dev/null 2>&1; then
      echo "FATAL: listed meeting $m missing $CAM.avi or Mix-Headset.wav on mirror" >&2
      exit 1
    fi
  done
  SELECTED=("${LISTED[@]}")
  export AMI_SELECTION_RULE="explicit set $SETNAME ($MEETING_LIST): first $N sorted = measured, rest warm"
else
  # Candidate scenario meetings, generated already sorted.
  CANDIDATES=()
  for s in $(seq 2002 2016); do for x in a b c d; do CANDIDATES+=("ES${s}${x}"); done; done
  for s in $(seq 1000 1009); do for x in a b c d; do CANDIDATES+=("IS${s}${x}"); done; done
  for s in $(seq 3003 3012); do for x in a b c d; do CANDIDATES+=("TS${s}${x}"); done; done

  # Probe the mirror in candidate order until OFFSET+TOTAL meetings that have
  # BOTH files are collected. Skip-missing keeps selection deterministic.
  NEED=$(( OFFSET + TOTAL ))
  PICKED=()
  for m in "${CANDIDATES[@]}"; do
    [ "${#PICKED[@]}" -ge "$NEED" ] && break
    if curl -sIf "$BASE/$m/video/$m.$CAM.avi" >/dev/null 2>&1 \
    && curl -sIf "$BASE/$m/audio/$m.Mix-Headset.wav" >/dev/null 2>&1; then
      PICKED+=("$m")
    else
      echo "  skip $m (not on mirror)"
    fi
  done
  if [ "${#PICKED[@]}" -lt "$NEED" ]; then
    echo "FATAL: only ${#PICKED[@]} meetings available, need $NEED (offset $OFFSET + $TOTAL)." >&2
    exit 1
  fi
  SELECTED=("${PICKED[@]:$OFFSET:$TOTAL}")
fi
LAST="${SELECTED[$(( ${#SELECTED[@]} - 1 ))]}"   # bash-3.2 safe (mac smoke runs)
echo "selected ${#SELECTED[@]} meetings: ${SELECTED[0]} .. $LAST"

mkdir -p "$DEST" "$CACHE"
rm -f "$DEST"/*.avi "$DEST/SHA256SUMS" "$DEST/corpus_manifest.json"

for m in "${SELECTED[@]}"; do
  vid="$CACHE/$m.$CAM.avi"
  wav="$CACHE/$m.Mix-Headset.wav"
  if [ ! -s "$vid" ]; then
    echo "downloading $m.$CAM.avi (cached in $CACHE after first use)"
    curl -fsSL --max-time 1800 "$BASE/$m/video/$m.$CAM.avi" -o "$vid"
  fi
  if [ ! -s "$wav" ]; then
    echo "downloading $m.Mix-Headset.wav"
    curl -fsSL --max-time 1800 "$BASE/$m/audio/$m.Mix-Headset.wav" -o "$wav"
  fi
  # Stream copy: no re-encode, so the mux is cheap and bit-stable given the
  # same inputs. Metadata stripped so mux output never varies by tool version
  # string. PCM audio in AVI is a first-class combination.
  "$FFMPEG" -nostdin -loglevel error -y \
    -i "$vid" -i "$wav" \
    -map 0:v:0 -map 1:a:0 -c copy \
    -map_metadata -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
    "$DEST/$m.avi"
  echo "  muxed $m.avi"
done

python3 - "$DEST" "$CACHE" "$N" "$OFFSET" "$EXTRA" "$CAM" "${SELECTED[@]}" <<'PY'
import json, os, pathlib, sys, wave
dest, cache = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
n, offset, extra, cam = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
meetings = sys.argv[7:]
durations = {}
for m in meetings:
    with wave.open(str(cache / f"{m}.Mix-Headset.wav"), "rb") as w:
        durations[m] = round(w.getnframes() / w.getframerate(), 2)
(dest / "corpus_manifest.json").write_text(json.dumps({
    "corpus": "ami",
    "source": "groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus",
    "camera": cam,
    "audio": "Mix-Headset",
    "mux": "ffmpeg stream copy (DivX video + PCM wav -> AVI), bitexact, metadata stripped",
    "selection_rule": os.environ.get(
        "AMI_SELECTION_RULE",
        "first N sorted scenario-meeting IDs "
        "(ES2002-16, IS1000-09, TS3003-12, sessions a-d), "
        "skipping IDs absent from the mirror"),
    "n_measured": n,
    "n_warm_extra": extra,
    "offset": offset,
    "docs_measured": [f"{m}.avi" for m in meetings[:n]],
    "docs_warm": [f"{m}.avi" for m in meetings[n:]],
    "duration_s": {f"{m}.avi": durations[m] for m in meetings},
    "total_measured_audio_s": round(sum(durations[m] for m in meetings[:n]), 2),
}, indent=1))
total = sum(durations[m] for m in meetings[:n])
print(f"manifest written: {n} measured ({total/3600:.2f} h of meeting audio) + {extra} warm")
PY

cd "$DEST"
sha256sum *.avi > SHA256SUMS
sha256sum -c --quiet SHA256SUMS
# Embed the sha map into the manifest so the corpus_pin gate can verify
# every run's input bytes against the pinned corpus (added 2026-08-21).
python3 - <<'PY'
import json, pathlib
mf = pathlib.Path("corpus_manifest.json")
m = json.loads(mf.read_text())
shas = {}
for line in open("SHA256SUMS"):
    h, name = line.split(None, 1)
    name = name.strip().lstrip("*")
    shas[name] = {"sha256": h, "bytes": pathlib.Path(name).stat().st_size}
m["sha256"] = shas
mf.write_text(json.dumps(m, indent=1))
print(f"manifest: sha256 map embedded for {len(shas)} files")
PY
echo
echo "corpus ready: $DEST"
echo "  docs      : $(ls -1 ./*.avi | wc -l | tr -d ' ')"
echo "  manifest  : $DEST/corpus_manifest.json"
echo "  sha256sums: $DEST/SHA256SUMS (verified)"
