#!/usr/bin/env python3
"""JIT stager for the archive_films_v2 corpus, ON THE BOX.

Walks corpus/sets/archive_films_queue.txt (deterministic: downloads desc,
identifier tie-break) SEQUENTIALLY, fetching each item's metadata
just-in-time — staging does not depend on the full-collection census.

Per candidate, in order (any failure -> one journaled REJECT, next item):

  gate    license: EXPLICIT allowlist (CC family + public-domain marks);
          items with no license metadata are rejected, never assumed PD
  gate    source-reported duration 60-240 min
  gate    deterministic MP4 derivative pick
          (h.264 > MPEG4 > 512Kb MPEG4; ties: longer, larger, name asc)
  gate    dedup vs accepted: normalized title AND duration within
          max(120 s, 2%) — remakes with different runtimes survive
  fetch   curl from archive.org (sequential = politeness; retries)
  probe   ffmpeg null stream-copy -> video_duration_s, frames, fps
          (+ has_audio, INFORMATIONAL only: the benchmark pipe is
          detect-only, so silent prints are acceptable by design)
  gate    probe corroboration: video stream within max(300 s, 10%) of the
          source-reported duration (broken metadata, truncated downloads)
  record  sha256 + bytes BEFORE upload
  upload  -> provisional versioned prefix; byte size verified via
          head-object after the copy
  journal EVERY decision (accepted + rejected) -> resumable jsonl;
          relaunch skips journaled ids and continues

Stops at TARGET accepted (then run/freeze_films.py pins the corpus) or on
queue exhaustion (exit 3 — extend the queue with make_films_queue.py --q
<bigger> and relaunch; rejects are never retried, delete their journal
lines to force a retry).

  --dry-run N   metadata gates only, first N candidates, no journal, no
                downloads — acceptance-rate preview, laptop-safe

Env: TARGET (500), S3_BUCKET, S3_KEY_PREFIX, JOURNAL, WORK, AWS_BIN, FFMPEG.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "corpus" / "sets" / "archive_films_queue.txt"
JOURNAL = Path(os.environ.get("JOURNAL", str(Path.home() / "stage_films_v2_journal.jsonl")))
WORK = Path(os.environ.get("WORK", str(Path.home() / "stage_films_v2_tmp")))
BUCKET = os.environ.get("S3_BUCKET", "rocketride-benchmark-data")
KEY_PREFIX = os.environ.get("S3_KEY_PREFIX", "leela/corpus/archive_films_v2")
TARGET = int(os.environ.get("TARGET", "500"))
AWS = os.environ.get("AWS_BIN") or "aws"
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
GOOD_FORMATS = ("h.264", "MPEG4", "512Kb MPEG4")

LICENSE_ALLOW = (
    "creativecommons.org/publicdomain/mark",
    "creativecommons.org/publicdomain/zero",
    "creativecommons.org/licenses/publicdomain",
    "creativecommons.org/licenses/by",
)


def license_ok(url):
    u = re.sub(r"^https?://(www\.)?", "", str(url or "").strip().lower())
    return any(u.startswith(a) for a in LICENSE_ALLOW)


def http_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))


def parse_len(ln):
    try:
        if ":" in str(ln):
            p = [float(x) for x in str(ln).split(":")]
            return sum(v * 60 ** i for i, v in enumerate(reversed(p)))
        return float(ln)
    except (ValueError, TypeError):
        return 0.0


def norm_title(t):
    t = re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()
    return re.sub(r"\b(the|a|an)\b", "", t).strip()


def pick_mp4(files):
    cands = []
    for f in files:
        name = f.get("name", "")
        fmt = f.get("format", "")
        if not name.lower().endswith(".mp4") or fmt not in GOOD_FORMATS:
            continue
        cands.append((GOOD_FORMATS.index(fmt), -parse_len(f.get("length")),
                      -int(f.get("size", 0) or 0), name, f))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:4])
    return cands[0][4]


def probe(path):
    """null stream-copy pass -> (rc, video_duration_s, frames, fps, has_audio)"""
    try:
        p = subprocess.run(
            [FFMPEG, "-nostdin", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
            capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return 1, 0.0, 0, 0.0, False
    t = (p.stderr or "") + (p.stdout or "")
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", t)
    h, mi, se = times[-1] if times else ("0", "0", "0")
    vd = round(int(h) * 3600 + int(mi) * 60 + float(se), 2)
    frames = re.findall(r"frame=\s*(\d+)", t)
    fps = re.search(r"([\d.]+) fps", t)
    has_audio = bool(re.search(r"Stream #.*Audio:", t))
    return p.returncode, vd, int(frames[-1]) if frames else 0, \
        float(fps.group(1)) if fps else 0.0, has_audio


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def download(ident, fname, dest):
    url = f"https://archive.org/download/{ident}/{urllib.parse.quote(fname)}"
    r = subprocess.run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "10",
                        "--max-time", "7200", url, "-o", str(dest)])
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def upload(local, doc):
    key = f"{KEY_PREFIX}/{doc}"
    if subprocess.run([AWS, "s3", "cp", str(local), f"s3://{BUCKET}/{key}", "--quiet"]).returncode != 0:
        return False
    h = subprocess.run([AWS, "s3api", "head-object", "--bucket", BUCKET, "--key", key],
                       capture_output=True, text=True)
    if h.returncode != 0:
        return False
    try:
        return json.loads(h.stdout).get("ContentLength") == local.stat().st_size
    except json.JSONDecodeError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", type=int, default=0, metavar="N",
                    help="metadata gates only for the first N candidates; no journal, no downloads")
    a = ap.parse_args()

    queue = [l.split("\t")[0].strip() for l in open(QUEUE)
             if not l.startswith("#") and l.strip()]

    journal = {}
    if not a.dry_run and JOURNAL.exists():
        for line in open(JOURNAL):
            try:
                r = json.loads(line)
                journal[r["id"]] = r
            except json.JSONDecodeError:
                pass
    accepted = [journal[i] for i in queue
                if i in journal and journal[i].get("decision") == "accepted"]
    print(f"queue {len(queue)}, journaled {len(journal)}, "
          f"accepted {len(accepted)}/{TARGET}", flush=True)

    out = None
    if not a.dry_run:
        WORK.mkdir(exist_ok=True)
        out = open(JOURNAL, "a")
    tallies = {}

    def decide(ident, decision, reason=None, extra=None):
        rec = {"id": ident, "decision": decision,
               **({"reason": reason} if reason else {}), **(extra or {})}
        tallies[reason or decision] = tallies.get(reason or decision, 0) + 1
        if a.dry_run:
            print(f"  {decision.upper():8s} {ident}" + (f" ({reason})" if reason else ""), flush=True)
        else:
            out.write(json.dumps(rec) + "\n")
            out.flush()
            os.fsync(out.fileno())
            if decision == "rejected":
                print(f"REJECT {ident} ({reason})", flush=True)
        return rec

    n_seen = 0
    for ident in queue:
        if len(accepted) >= TARGET:
            break
        if a.dry_run and n_seen >= a.dry_run:
            break
        if not a.dry_run and ident in journal:
            continue
        n_seen += 1

        m = http_json(f"https://archive.org/metadata/{ident}")
        if not m or "files" not in m:
            decide(ident, "rejected", "dark")
            continue
        md = m.get("metadata", {})
        lic = md.get("licenseurl", "")
        if not license_ok(lic):
            decide(ident, "rejected", "license", {"license": lic})
            continue
        best = pick_mp4(m["files"])
        if best is None:
            decide(ident, "rejected", "no_mp4")
            continue
        dur = parse_len(best.get("length"))
        if not (3600 <= dur <= 14400):
            decide(ident, "rejected", "duration", {"duration_s": round(dur, 2)})
            continue
        title = md.get("title", ident)
        nt = norm_title(title)
        dup = next((x for x in accepted if x.get("norm_title") == nt
                    and abs(x["duration_s"] - dur) <= max(120, 0.02 * dur)), None)
        if dup is not None:
            decide(ident, "rejected", "duplicate", {"duplicate_of": dup["id"]})
            continue

        if a.dry_run:
            accepted.append(decide(ident, "accepted", None,
                                   {"norm_title": nt, "duration_s": round(dur, 2)}))
            continue

        doc = f"{ident}.mp4"
        local = WORK / doc
        if not download(ident, best["name"], local):
            local.unlink(missing_ok=True)
            decide(ident, "rejected", "download_failed")
            continue
        rc, vd, frames, fps, has_audio = probe(local)
        if rc != 0 or vd <= 0:
            local.unlink(missing_ok=True)
            decide(ident, "rejected", "probe_failed")
            continue
        if abs(vd - dur) > max(300, 0.10 * dur):
            local.unlink(missing_ok=True)
            decide(ident, "rejected", "probe_duration_mismatch",
                   {"duration_s": round(dur, 2), "video_duration_s": vd})
            continue
        sha = sha256_file(local)
        nbytes = local.stat().st_size
        if not upload(local, doc):
            local.unlink(missing_ok=True)
            decide(ident, "rejected", "upload_failed")
            continue
        rec = decide(ident, "accepted", None, {
            "seq": len(accepted) + 1, "doc": doc, "src_file": best["name"],
            "format": best["format"], "license": lic, "title": title,
            "norm_title": nt, "duration_s": round(dur, 2),
            "video_duration_s": vd, "frames_counted": frames,
            "nominal_fps": fps, "has_audio": has_audio,
            "bytes": nbytes, "sha256": sha,
        })
        accepted.append(rec)
        local.unlink(missing_ok=True)
        print(f"ACCEPT {doc} ({len(accepted)}/{TARGET}, vdur={vd}s, "
              f"{nbytes / 1e9:.2f} GB)", flush=True)

    print(f"tallies: {json.dumps(tallies)}", flush=True)
    if a.dry_run:
        print(f"dry-run: {sum(1 for _ in accepted)} of first {n_seen} "
              f"candidates would be accepted", flush=True)
        return
    if len(accepted) >= TARGET:
        print(f"DONE: {len(accepted)} accepted -> run freeze_films.py", flush=True)
    else:
        print(f"QUEUE EXHAUSTED at {len(accepted)}/{TARGET} — extend "
              f"archive_films_queue.txt (make_films_queue.py --q <bigger>) and relaunch", flush=True)
        sys.exit(3)


if __name__ == "__main__":
    main()
