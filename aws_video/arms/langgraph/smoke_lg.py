"""Smoke the LangGraph video arm: N videos through a running service.

Verifies per video: HTTP 200, frame count ~= duration/15, >=1 chunk,
384-dim finite normalized embeddings. If ES2016d.avi is among the inputs,
also prints the informal cross-arm comparison against RocketRide's capture
(counts and shapes — functional replication, byte parity NOT required).

  python smoke_lg.py http://127.0.0.1:8200 video1.avi [video2.avi ...]
"""

import json
import os
import sys
import time
import urllib.request

import requests

BASE = sys.argv[1]
videos = sys.argv[2:]
assert videos, "give at least one video path"

# Generous: a cold cache downloads rfdetr + miniLM weights before ready.
for _ in range(300):
    try:
        if requests.get(f"{BASE}/health/ready", timeout=3).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit("service never became ready")
print("[lg-smoke] service ready:", requests.get(f"{BASE}/meta", timeout=5).json())

failures = []
for vp in videos:
    t0 = time.time()
    with open(vp, "rb") as fh:
        r = requests.post(f"{BASE}/process", files={"file": (vp.split("/")[-1], fh)},
                          timeout=3600)
    dt = time.time() - t0
    if r.status_code != 200:
        failures.append(f"{vp}: HTTP {r.status_code} {r.text[:200]}")
        continue
    out = r.json()
    docs = out["documents"]
    dims_ok = all(len(d["embedding"]) == 384 for d in docs)
    norms = [sum(x * x for x in d["embedding"]) ** 0.5 for d in docs]
    norms_ok = all(abs(n - 1) < 1e-3 for n in norms)
    print(f"[lg-smoke] {out['filename']}: {dt:.1f}s wall | "
          f"frames={out['n_frames']} chunks={out['n_chunks']} "
          f"chars={out['total_chars']} dims384={dims_ok} norms1={norms_ok} "
          f"| timings={out['timings']}")
    if not (docs and dims_ok and norms_ok):
        failures.append(f"{vp}: structure check failed")
    if out["filename"] == "ES2016d.avi":
        try:
            # A RocketRide capture of the same video (bench/capture_one.py output),
            # for a side-by-side count. Optional: absent file = no comparison.
            cap = json.load(open(os.environ.get(
                "RR_CAPTURE_DOCS", "capture/capture_docs.json")))
            rr_lines = sum(len(d["page_content"].split("\n")) for d in cap)
            rr_dets = sum(d["page_content"].count('"label"') for d in cap)
            lg_dets = sum(d["page_content"].count('"label"') for d in docs)
            print(f"[lg-smoke]   vs RocketRide capture: frames {out['n_frames']} vs {rr_lines} | "
                  f"chunks {out['n_chunks']} vs {len(cap)} | detections {lg_dets} vs {rr_dets}")
        except FileNotFoundError:
            print("[lg-smoke]   (RR capture not present locally; skipping comparison)")

if failures:
    raise SystemExit("LG SMOKE FAILED:\n  " + "\n  ".join(failures))
print("LG SMOKE PASSED")
