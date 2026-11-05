#!/usr/bin/env python3
"""Stock vs patched parse node on the same documents: per-document node self time and output identity.

    python3 bench/parse_ab_compare.py results/pathology-parse2745-stock-<ts>/rr/rep1 \
                                      results/pathology-parse2745-patched-<ts>/rr/rep1

Reads per_doc.jsonl (one record per document, with chunk_sha256) and flow_events.jsonl
(apaevt_flow enter/leave per component, client-stamped) from each run. Self time is
EXCLUSIVE (nested children subtracted), attributed to the document whose
[submit, completion] window holds the call -- exact only in seq mode, which is what
parse_pathology.sh runs.
"""
import collections
import json
import sys
from pathlib import Path


def load(run):
    run = Path(run)
    rows = [json.loads(l) for l in (run / "per_doc.jsonl").open() if l.strip()]
    docs = {r["doc"]: r for r in rows if not r.get("kind") and r.get("doc")}
    ev_path = run / "flow_events.jsonl"
    ev = [json.loads(l) for l in ev_path.open() if l.strip()] if ev_path.exists() else []
    spans = sorted((d["submit_ns"], d["completion_ns"], name) for name, d in docs.items()
                   if d.get("submit_ns") and d.get("completion_ns"))

    def owner(ts):
        for a, b, name in spans:
            if a <= ts <= b:
                return name
        return None

    stacks = collections.defaultdict(list)
    self_by = collections.defaultdict(lambda: collections.defaultdict(float))
    for e in ev:
        p, op, c, ts = e["pipe"], e["op"], e["component"], e["ts_ns"]
        if op == "enter":
            stacks[p].append([c, ts, 0])
        elif op == "leave" and stacks[p]:
            comp, t0, child = stacks[p].pop()
            dur = ts - t0
            who = owner(t0)
            if who:
                self_by[who][comp] += (dur - child) / 1e9
            if stacks[p]:
                stacks[p][-1][2] += dur
    return docs, self_by


def main(a, b):
    sa, selfa = load(a)
    sb, selfb = load(b)
    names = sorted(set(sa) | set(sb), key=lambda n: -(sa.get(n, {}).get("completion_ns", 0) - sa.get(n, {}).get("submit_ns", 0)))
    print("%-14s %8s %8s | %9s %9s %8s | %9s %9s | %s" % (
        "doc", "chunks A", "chunks B", "parse A s", "parse B s", "speedup", "total A", "total B", "identical chunks"))
    same = diff = 0
    for n in names:
        da, db = sa.get(n, {}), sb.get(n, {})
        pa = selfa.get(n, {}).get("parse_1", 0.0)
        pb = selfb.get(n, {}).get("parse_1", 0.0)
        ta = (da.get("completion_ns", 0) - da.get("submit_ns", 0)) / 1e9 if da else 0
        tb = (db.get("completion_ns", 0) - db.get("submit_ns", 0)) / 1e9 if db else 0
        ident = "-"
        if da.get("ok") and db.get("ok"):
            ident = "yes" if da.get("chunk_sha256") == db.get("chunk_sha256") else "NO"
            same += ident == "yes"; diff += ident == "NO"
        elif da or db:
            ident = f"A ok={da.get('ok')} B ok={db.get('ok')}"
        sp = f"{pa / pb:8.0f}x" if pb > 0 and pa > 0 else "-"
        print("%-14s %8s %8s | %9.1f %9.2f %8s | %9.1f %9.1f | %s" % (
            n, da.get("n_chunks"), db.get("n_chunks"), pa, pb, sp, ta, tb, ident))
    print(f"\nchunk hashes identical on {same} documents, different on {diff}")
    for arm, s in (("A", selfa), ("B", selfb)):
        tot = collections.defaultdict(float)
        for d in s.values():
            for k, v in d.items():
                tot[k] += v
        print(f"{arm} node self time (s): " + ", ".join(f"{k}={v:.1f}" for k, v in sorted(tot.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
