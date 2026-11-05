#!/usr/bin/env python3
"""Freeze the archive_films_v2 corpus, ON THE BOX, after stage_films_jit.py
reports DONE.

  1  the final set = first TARGET accepted journal records in queue order
     (sequential staging makes queue order == acceptance order)
  2  verify EVERY S3 object exists with the journaled byte size (head-object
     per object — no re-download)
  3  SHA-pinned manifest -> s3://<prefix>/corpus_manifest.json, its own
     sha256 beside it (corpus_manifest.sha256) — the freeze seal
  4  nested subset pins 10 ⊂ 100 ⊂ TARGET (prefixes of acceptance order)
     + durations json -> corpus/sets/ AND s3://<prefix>/pins/ (fetch to the
     laptop and commit; the commit is the freeze)

Exits nonzero without writing anything if fewer than TARGET accepted or any
object fails verification.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "corpus" / "sets" / "archive_films_queue.txt"
SETS = ROOT / "corpus" / "sets"
JOURNAL = Path(os.environ.get("JOURNAL", str(Path.home() / "stage_films_v2_journal.jsonl")))
BUCKET = os.environ.get("S3_BUCKET", "rocketride-benchmark-data")
KEY_PREFIX = os.environ.get("S3_KEY_PREFIX", "leela/corpus/archive_films_v2")
TARGET = int(os.environ.get("TARGET", "500"))
AWS = os.environ.get("AWS_BIN") or "aws"
SUBSETS = (10, 100, TARGET)


def main():
    queue = [l.split("\t")[0].strip() for l in open(QUEUE)
             if not l.startswith("#") and l.strip()]
    recs = {}
    for line in open(JOURNAL):
        try:
            r = json.loads(line)
            recs[r["id"]] = r
        except json.JSONDecodeError:
            pass
    accepted = [recs[i] for i in queue
                if i in recs and recs[i].get("decision") == "accepted"]
    if len(accepted) < TARGET:
        sys.exit(f"only {len(accepted)}/{TARGET} accepted — not freezing")
    final = accepted[:TARGET]

    print(f"verifying {len(final)} S3 objects against the journal ...", flush=True)
    bad = []
    for i, r in enumerate(final, 1):
        h = subprocess.run(
            [AWS, "s3api", "head-object", "--bucket", BUCKET,
             "--key", f"{KEY_PREFIX}/{r['doc']}"],
            capture_output=True, text=True)
        ok = False
        if h.returncode == 0:
            try:
                ok = json.loads(h.stdout).get("ContentLength") == r["bytes"]
            except json.JSONDecodeError:
                pass
        if not ok:
            bad.append(r["doc"])
        if i % 100 == 0:
            print(f"  {i}/{len(final)}", flush=True)
    if bad:
        sys.exit(f"VERIFY FAILED for {len(bad)} objects: {bad[:10]}")

    manifest = {
        "corpus": "archive_films_v2",
        "frozen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_docs": TARGET,
        "selection_rule": "JIT queue (downloads desc, identifier asc; snapshot "
                          "in queue header): explicit CC/PD license allowlist, "
                          "source duration 60-240 min, deterministic mp4 pick, "
                          "title+duration-proximity dedup, probe-corroborated "
                          "duration; first TARGET accepted, sequential order",
        "nested_subsets": {str(n): f"archive_films_{n}.txt = first {n} accepted"
                           for n in SUBSETS},
        "duration_s": {r["doc"]: r["duration_s"] for r in final},
        "video_duration_s": {r["doc"]: r["video_duration_s"] for r in final},
        "video_fps_probe": {r["doc"]: {"frames_counted": r["frames_counted"],
                                       "nominal_fps": r["nominal_fps"]}
                            for r in final},
        "license": {r["doc"]: r["license"] for r in final},
        "has_audio": {r["doc"]: r["has_audio"] for r in final},
        "sha256": {r["doc"]: {"sha256": r["sha256"], "bytes": r["bytes"]}
                   for r in final},
        "total_hours": round(sum(r["duration_s"] for r in final) / 3600, 2),
        "total_gb": round(sum(r["bytes"] for r in final) / 1e9, 1),
        "note": "duration_s = source metadata (footage denominator); "
                "video_duration_s = ffmpeg-probed video stream (frame_law "
                "denominator); has_audio informational — detect-only pipe",
    }
    mtxt = json.dumps(manifest, indent=1)
    msha = hashlib.sha256(mtxt.encode()).hexdigest()

    SETS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    pins = []
    for n in SUBSETS:
        p = SETS / f"archive_films_{n}.txt"
        with open(p, "w") as f:
            f.write(f"# archive_films_v2 nested subset {n}/{TARGET} — first {n} "
                    f"accepted in queue order; frozen {stamp}; manifest sha {msha}\n")
            for r in final[:n]:
                f.write(f"{r['id']}\t{r['src_file']}\n")
        pins.append(p)
    durs = SETS / f"archive_films_{TARGET}_durations.json"
    durs.write_text(json.dumps({r["doc"]: r["duration_s"] for r in final}, indent=1))
    pins.append(durs)

    mlocal = Path.home() / "corpus_manifest.json"
    mlocal.write_text(mtxt)
    for local, key in [(mlocal, f"{KEY_PREFIX}/corpus_manifest.json")] + \
                      [(p, f"{KEY_PREFIX}/pins/{p.name}") for p in pins]:
        if subprocess.run([AWS, "s3", "cp", str(local), f"s3://{BUCKET}/{key}",
                           "--quiet"]).returncode != 0:
            sys.exit(f"upload failed: {key}")
    # aws CLI needs a seekable --body; stdin is not (bitten on the first freeze)
    slocal = Path.home() / "corpus_manifest.sha256"
    slocal.write_text(msha + "\n")
    if subprocess.run([AWS, "s3", "cp", str(slocal),
                       f"s3://{BUCKET}/{KEY_PREFIX}/corpus_manifest.sha256",
                       "--quiet"]).returncode != 0:
        sys.exit("upload failed: corpus_manifest.sha256")

    print(f"FROZEN: {TARGET} docs, {manifest['total_hours']} h, "
          f"{manifest['total_gb']} GB; manifest sha256 {msha}", flush=True)
    print(f"pins in s3://{BUCKET}/{KEY_PREFIX}/pins/ — fetch and commit "
          f"from the laptop", flush=True)


if __name__ == "__main__":
    main()
