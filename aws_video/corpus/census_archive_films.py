#!/usr/bin/env python3
"""Census + selection for the archive_films corpus — the ami_eda HEAD-sweep
equivalent for Internet Archive `feature_films`. No video downloads: the
archive.org metadata API already carries per-file duration ("length"),
size, and format for every item.

Two phases, both resumable:

  1) CENSUS   enumerate collection:feature_films via the scrape API
              (cursor-paginated), then fetch each item's /metadata and
              keep items that have a playable derivative. One JSONL line
              per item -> census.jsonl (~28k items, ~30-60 min at 12
              threads; safe to re-run, it skips ids already censused).
  2) SELECT   filter + pin the corpus set, mirroring corpus/sets/ami_full.txt:
                - duration 60..240 min (the 1-hour-plus requirement,
                  capped so one 10-hour outlier can't distort waves)
                - has an h.264 (preferred) or MPEG4 mp4 derivative
                - not in the silent_films subcollection (dual-lane needs
                  speech; a final ffprobe audio gate runs at staging)
                - deduped by normalized title (the collection has re-uploads)
              then rank by all-time downloads (popularity ~ print quality
              proxy; snapshot recorded) and emit the top N.

Outputs (committed; the pinned set is the corpus contract):
  corpus/sets/archive_films.txt             identifier<TAB>filename, one per doc
  corpus/sets/archive_films_durations.json  {"<identifier>.mp4": seconds}
  corpus/census_archive_films.jsonl         full census (all items, for EDA)

Usage:
  python3 corpus/census_archive_films.py            # census + select N=500
  python3 corpus/census_archive_films.py --n 1000   # bigger corpus
  python3 corpus/census_archive_films.py --census-only
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
CENSUS = HERE / "census_archive_films.jsonl"
SETS = HERE / "sets"
COLLECTION = "feature_films"
SCRAPE = "https://archive.org/services/search/v1/scrape"
GOOD_FORMATS = ("h.264", "MPEG4", "512Kb MPEG4")  # preference order

def http_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))

def enumerate_items():
    """All identifiers in the collection (movies only, skips subcollections)."""
    ids, cursor = [], None
    while True:
        q = {"q": f"collection:{COLLECTION} AND mediatype:movies",
             "fields": "identifier,downloads", "count": "10000"}
        if cursor:
            q["cursor"] = cursor
        d = http_json(f"{SCRAPE}?{urllib.parse.urlencode(q)}")
        if d is None:
            sys.exit("scrape API failed")
        ids += [(it["identifier"], it.get("downloads", 0)) for it in d["items"]]
        cursor = d.get("cursor")
        if not cursor:
            return ids

def parse_len(ln):
    try:
        if ":" in str(ln):
            p = [float(x) for x in str(ln).split(":")]
            return sum(v * 60 ** i for i, v in enumerate(reversed(p)))
        return float(ln)
    except (ValueError, TypeError):
        return 0.0

def census_item(args):
    ident, downloads = args
    m = http_json(f"https://archive.org/metadata/{ident}")
    if not m or "files" not in m:          # dark / withdrawn item
        return {"id": ident, "dark": True}
    best = None
    for f in m["files"]:
        if not f["name"].lower().endswith(".mp4"):
            continue
        fmt = f.get("format", "")
        if fmt not in GOOD_FORMATS:
            continue
        dur = parse_len(f.get("length"))
        cand = {"file": f["name"], "format": fmt, "duration_s": round(dur, 2),
                "bytes": int(f.get("size", 0))}
        # prefer h.264; among equals prefer the longer/larger (full print)
        if (best is None
                or (GOOD_FORMATS.index(fmt) < GOOD_FORMATS.index(best["format"]))
                or (fmt == best["format"] and dur > best["duration_s"])):
            best = cand
    md = m.get("metadata", {})
    colls = md.get("collection", [])
    return {
        "id": ident, "downloads": downloads,
        "title": md.get("title", ident),
        "year": md.get("year") or md.get("date", ""),
        "license": md.get("licenseurl", ""),
        "collections": colls if isinstance(colls, list) else [colls],
        **({"video": best} if best else {"no_mp4": True}),
    }

def run_census():
    done = set()
    if CENSUS.exists():
        for line in open(CENSUS):
            try:
                done.add(json.loads(line)["id"])
            except json.JSONDecodeError:
                pass
    items = [(i, d) for i, d in enumerate_items() if i not in done]
    # downloads-descending: selection is top-N-by-downloads, so once the
    # censused set covers N eligible + reserves, --select-only is provably
    # identical to a full census — no need to wait 8h for the tail.
    items.sort(key=lambda x: -(x[1] or 0))
    print(f"census: {len(done)} already done, {len(items)} to fetch (downloads-desc)")
    with open(CENSUS, "a") as out, ThreadPoolExecutor(12) as ex:
        for n, rec in enumerate(ex.map(census_item, items), 1):
            out.write(json.dumps(rec) + "\n")
            if n % 500 == 0:
                out.flush()
                print(f"  {n}/{len(items)}")
    print("census complete")

def norm_title(t):
    t = re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()
    return re.sub(r"\b(the|a|an)\b", "", t).strip()

def select(n):
    rows = [json.loads(l) for l in open(CENSUS)]
    pool, seen_titles = [], set()
    stats = {"dark": 0, "no_mp4": 0, "silent": 0, "duration": 0, "dup": 0}
    for r in sorted(rows, key=lambda r: -r.get("downloads", 0)):
        if r.get("dark"):
            stats["dark"] += 1; continue
        if "video" not in r:
            stats["no_mp4"] += 1; continue
        if "silent_films" in r.get("collections", []):
            stats["silent"] += 1; continue
        if not (3600 <= r["video"]["duration_s"] <= 14400):
            stats["duration"] += 1; continue
        t = norm_title(r["title"])
        if t in seen_titles:
            stats["dup"] += 1; continue
        seen_titles.add(t)
        pool.append(r)
    print(f"filtered out: {stats}; eligible pool: {len(pool)}")
    picked = pool[:n]
    if len(picked) < n:
        print(f"WARNING: pool smaller than requested n={n}")

    SETS.mkdir(exist_ok=True)
    hdr = (f"# archive_films — top {len(picked)} by downloads of the eligible pool "
           f"(census {time.strftime('%Y-%m-%d')}; 60-240 min, mp4, non-silent, deduped)\n")
    with open(SETS / "archive_films.txt", "w") as f:
        f.write(hdr)
        for r in picked:
            f.write(f"{r['id']}\t{r['video']['file']}\n")
    durs = {f"{r['id']}.mp4": r["video"]["duration_s"] for r in picked}
    (SETS / "archive_films_durations.json").write_text(json.dumps(durs, indent=1))
    hours = sum(durs.values()) / 3600
    gb = sum(r["video"]["bytes"] for r in picked) / 1e9
    print(f"pinned {len(picked)} films: {hours:.0f} h, ~{gb:.0f} GB "
          f"-> corpus/sets/archive_films.txt + archive_films_durations.json")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--census-only", action="store_true")
    ap.add_argument("--select-only", action="store_true",
                    help="reuse the existing census.jsonl")
    a = ap.parse_args()
    if not a.select_only:
        run_census()
    if not a.census_only:
        select(a.n)
