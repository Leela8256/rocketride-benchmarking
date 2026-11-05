"""Concurrency-invariance: does an arm produce the SAME bytes at C=128 as at C=1?

This is a DIFFERENT property from run-to-run determinism, and a cheaper one to
buy. Rep-to-rep determinism needs two runs at the same concurrency and answers
"is this repeatable". This answers "does concurrency change the output", which
is the question that actually matters for a threaded pipeline: it is the only
check that can see races in chunk ordering, buffer reuse across requests, or
batch mis-attribution.

Nothing else in the suite catches that class. Such output is well-formed
(384 dims, finite, unit-norm), correctly counted, and attributed to the right
document -- it is simply wrong. That is exactly how the transposition defect
survived six runs.

It is NOT a strict substitute for rep-to-rep determinism: two runs at the same
concurrency could still differ. It is strictly stronger evidence than one run
alone, and it costs one extra phase rather than a whole extra repetition.

  python3 bench/cross_mode_determinism.py <run_a_dir> <run_b_dir>

Compares the documents present in BOTH runs, per arm, independently. The two
runs may differ in size -- a 1000-doc c128 run against a 200-doc sequential
run compares the 200 they share.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metrics.records import load_records, ok_records  # noqa: E402

ARMS = {"lg": "langgraph", "rr": "rocketride"}


def digests(run: Path, arm: str):
    """doc -> ordered chunk-hash list, for successfully completed docs only.

    A doc that failed in one mode carries no output to compare; it is reported
    as unproven rather than silently counted as matching. Vacuous agreement is
    not evidence.
    """
    f = run / arm / "rep1" / "per_doc.jsonl"
    if not f.exists():
        return None, f"missing {f}"
    rows, meta, _ = load_records(f)
    return ({r["doc"]: list(r.get("chunk_sha256") or [])
             for r in ok_records(rows)}, (meta or {}).get("mode"))


def compare(a: dict, b: dict):
    shared = sorted(set(a) & set(b))
    mismatch = [d for d in shared if a[d] != b[d]]
    return {
        "compared": len(shared),
        "identical": len(shared) - len(mismatch),
        "mismatched": len(mismatch),
        "mismatched_docs": mismatch[:25],
        "only_in_a": len(set(a) - set(b)),
        "only_in_b": len(set(b) - set(a)),
        # Vacuous is not a pass: zero shared docs proves nothing.
        "PASS": len(shared) > 0 and not mismatch,
    }


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    ra, rb = Path(sys.argv[1]), Path(sys.argv[2])
    out = {"run_a": ra.name, "run_b": rb.name, "arms": {}}

    for arm, name in ARMS.items():
        da, ma = digests(ra, arm)
        db, mb = digests(rb, arm)
        if da is None or db is None:
            out["arms"][name] = {"error": ma if da is None else mb}
            continue
        res = compare(da, db)
        res["mode_a"], res["mode_b"] = ma, mb
        out["arms"][name] = res

    (ra / "CROSS_MODE_DETERMINISM.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))

    print("\n" + "=" * 74)
    print(f"CONCURRENCY INVARIANCE  {ra.name}  vs  {rb.name}")
    for name, r in out["arms"].items():
        if r.get("error"):
            print(f"  {name:<12} ERROR: {r['error']}")
            continue
        print(f"  {name:<12} [{r['mode_a']} vs {r['mode_b']}]  "
              f"{r['identical']}/{r['compared']} identical  "
              f"mismatched={r['mismatched']}  PASS={r['PASS']}")
        if r["mismatched_docs"]:
            print(f"       MISMATCHED: {r['mismatched_docs']}")
    print("=" * 74)
    ok = all(r.get("PASS") for r in out["arms"].values())
    print("INVARIANT" if ok else "NOT INVARIANT — output depends on concurrency")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
