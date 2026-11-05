# LONG_VIDEO_SOURCES.md — where to get a corpus of 1-hour-plus videos

Scan done 2026-08-21 after FineVideo washed out (its videos cap at 11.3
min — see `finevideo_eda/`). Requirement: a **considerable number of
videos over 1 hour**, downloadable as real files (no YouTube scraping),
stageable to S3 through the same pipeline as `ami_full`
(`run/stage_corpus.sh` pattern: fetch → ffprobe → sha256 → upload →
manifest).

## 2026-08-22 UPDATE — the answer is MeetingBank

`feature_films` was rejected by Leela (wanted a proper research corpus,
"publicly available like AMI"). New scan found **MeetingBank** (ACL 2023,
Hu et al.): US city-council meetings from 6 cities — Seattle, Denver,
Boston, King County, Alameda, Long Beach. It is AMI's shape at 50× the
footage: a curated, citable meeting corpus with transcripts, and the
videos are **rehosted by the authors on archive.org** — direct HTTP, no
gate, no registration.

Full-census probe of all six archive.org items (every file's `length`
field, 2026-08-22):

| city | videos | size |
|---|---|---|
| meetingbank-seattle | 330 | 146 GB |
| meetingbank-denver | 770 | 1,365 GB |
| meetingbank-boston | 59 | 121 GB |
| meetingbank-king-county | 264 | 162 GB |
| meetingbank-alameda | 201 | 411 GB |
| meetingbank-long-beach | 513 | 894 GB |
| **total** | **2,137** | **3.1 TB, 5,416 h** |

- **≥ 1 h: 1,756 videos (82%)** · ≥ 2 h: 1,184 · median 2 h 10 m ·
  p25 77 min · p75 3 h 32 m · max ~9.8 h
- ~0.57 GB/h average bitrate; mp4 (some `.ia.mp4` archive re-encodes)
- Speech-rich (council meetings → dual-lane Whisper has real work);
  static chamber cameras suit RF-DETR person/object detection
- Zenodo bundle (CC-BY-4.0, 637 MB, record 7989108) has per-meeting
  metadata + word-level-timed transcripts → maps file → meeting → city,
  and doubles as an external reference for our transcript lane
- Caveats: a few files have missing/zero `length` metadata (probe at
  staging as always); dedupe/link via the Zenodo main JSON; audio-stream
  presence still gets verified in smoke; content is public-record
  government footage

Corpus sketch (`meetingbank1kh`): pick ~500 videos ≥ 1 h stratified by
city/duration ≈ 1,100 h ≈ 630 GB S3 (~$15/mo). Waves of ~20–25 videos
fit /dev/shm. Runtime bounds at AMI-measured rates: RR blast ~30 h/rep
(K=5 ≈ 6 h), LG ~7 h/rep. Staging = `fetch_archive.sh` pattern against
`archive.org/download/meetingbank-<city>/<file>`, unchanged otherwise.

The table below is the 2026-08-21 scan, kept for the record.

## The verdict

| source | videos ≥ 1 h | typical length | access | audio | fits staging |
|---|---|---|---|---|---|
| **Internet Archive `feature_films`** ← recommended | **~15–20k of 28,415 items** (sampled: 86% ≥60 min) | median 82 min, p25 70 / p75 99 | none — direct HTTP, public domain | yes (talkies; filter out silents) | identical to AMI mirror flow |
| **SoccerNet** | **500 games** (mux 2×~45-min halves/game, like the AMI mux) | ~1.5 h/game, 764 h total | free NDA form → password | broadcast commentary (verify in smoke) | yes; mkv 720p/224p @25 fps |
| Ego4D | unknown until licensed (3,670 h total; multi-hour recordings exist, avg video well under 1 h) | 5 min – 7 h | license, ~48 h approval, AWS creds, 14-day expiry | partial | S3-native but gated + TB-scale |
| Democracy Now (archive.org) | ~0 — episodes are 59 min | median 59 min (n=6,678) | none | yes | — misses the bar by 1 min |
| FineVideo | 0 | max 11.3 min | — | — | rejected |
| LVBench / Video-MME-long / MLVU / LongVideoBench | 103–300 | 30 min – 2 h | YouTube links only | — | rejected: tiny + scraping |
| YouTube-Commons ≥1 h CC-BY subset | plenty on paper | — | needs yt-dlp at scale from an EC2 IP | — | rejected: bot-blocked, unreproducible |
| EPIC-KITCHENS / Ego-Exo4D / MovieNet / MIT OCW | a handful / ≤42 min takes / no raw video / ~50-min lectures | — | — | — | rejected |

Numbers verified today: `feature_films` count and the duration sample
via the archive.org APIs (66 top-downloaded items probed:
min 17 / median 82 / max 661 min; 57/66 ≥ 60 min, 23/66 ≥ 90 min);
Democracy Now sample n=20 (median 59 min); SoccerNet facts from
soccer-net.org (500+50 broadcasts, 764 h, mkv 720p/224p, NDA form);
Ego4D access mechanics from ego4d-data.org docs.

## Recommendation: `archive_films` corpus off `feature_films`

Public-domain feature films are the only source that is simultaneously
(a) thousands of hour-plus videos, (b) zero-gate direct HTTP — the exact
AMI-mirror workflow we already have scripts for, and (c) speech-rich for
the dual-lane pipe. Proposal:

- **Size options**: 500 films ≈ 700 h (7× ami_full) or 1,000 films ≈
  1,400 h. At ~0.4–0.7 GB/h for the archive.org H.264 derivative that is
  roughly 300–500 GB (500 films) or 600 GB–1 TB (1,000) in S3
  ($7–23/mo). Waves of 20–40 films fit the 30 GB /dev/shm cap.
- **Selection/staging filters** (all enforceable in the fetch script from
  item metadata + ffprobe, recorded in the manifest):
  1. duration 60–240 min (drops shorts and the 11-hour outliers);
  2. has an audio stream (drops silent-era films — or keep a counted
     silent stratum deliberately);
  3. prefer the `h.264`/`.mp4` derivative, min height ≥ 360;
  4. dedupe by normalized title (the collection has re-uploads);
  5. pinned selection list + sha256 manifest → corpus_pin, as always.
- **Runtime math** at AMI-measured rates (upper bounds): 700 h → RR
  blast ~19 h/rep (K=5 engines ~4 h), LG ~4.7 h/rep. 1,400 h doubles it.
  Both regimes now exist: FineVideo-style small docs (if ever wanted),
  `ami_full` mid, `archive_films` long.

**SoccerNet as the optional second set**: 500 muxed ~1.5 h games —
uniform content, useful as a controlled contrast to the heterogeneous
films, and the half-mux is literally our AMI trick. Needs the (free) NDA
password first and an audio-presence check in smoke.

**Ego4D**: only worth pursuing if we want the egocentric/research-grade
story; start the license clock (~48 h) and size the ≥1 h tail from its
metadata after approval. Not the fast path.

## Next steps (when a source is picked)

1. `corpus/fetch_archive.sh` — clone of `fetch_ami.sh`: advancedsearch
   page → per-item metadata → pick best derivative → curl → ffprobe
   video duration → sha256 → S3, delete-as-you-go, resumable.
2. Full-collection duration/audio EDA from item metadata only (no video
   downloads — same trick as the AMI HEAD sweep) → pick the selection
   list, pin it.
3. `archive10` smoke: codec sanity (old MPEG-1/2 derivatives exist),
   webhook lane routing for .mp4, Whisper on vintage audio, frame counts
   at 15 s on 90-min films (~360 frames/video ≈ 2.6× AMI chunk mass).
