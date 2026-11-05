#!/usr/bin/env python3
"""Deterministic candidate queue for JIT staging of archive_films.

Staging must not wait on the full-collection census: selection order is
top-by-downloads, and the scrape API returns every (identifier, downloads)
pair in minutes. The queue IS the selection order — top Q movie items by
(downloads desc, identifier asc), committed to git. The JIT stager
(run/stage_films_jit.py) walks it in order, fetching per-item metadata
just-in-time, until TARGET candidates pass every gate.

Downloads counts are a moving popularity snapshot: determinism comes from
the committed queue file, not from re-running this script. The header
records the snapshot date. To extend an exhausted queue, re-run with a
bigger --q — already-journaled candidates are skipped by the stager, so
the extension only appends new work.

Usage: python3 corpus/make_films_queue.py [--q 1500]
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "sets" / "archive_films_queue.txt"
SCRAPE = "https://archive.org/services/search/v1/scrape"
COLLECTION = "feature_films"


def http_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", type=int, default=1500)
    a = ap.parse_args()
    ids, cursor = [], None
    while True:
        q = {"q": f"collection:{COLLECTION} AND mediatype:movies",
             "fields": "identifier,downloads", "count": "10000"}
        if cursor:
            q["cursor"] = cursor
        d = http_json(f"{SCRAPE}?{urllib.parse.urlencode(q)}")
        if d is None:
            sys.exit("scrape API failed")
        ids += [(it["identifier"], int(it.get("downloads") or 0)) for it in d["items"]]
        cursor = d.get("cursor")
        if not cursor:
            break
    ids.sort(key=lambda x: (-x[1], x[0]))
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w") as f:
        f.write(f"# archive_films JIT candidate queue — top {min(a.q, len(ids))} of "
                f"{len(ids)} movie items by (downloads desc, identifier asc); "
                f"downloads snapshot {time.strftime('%Y-%m-%d')}\n")
        for ident, dl in ids[:a.q]:
            f.write(f"{ident}\t{dl}\n")
    print(f"queue: {min(a.q, len(ids))} candidates -> {OUT} (collection pool {len(ids)})")


if __name__ == "__main__":
    main()
