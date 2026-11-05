"""Join diagnostic coverage and output identity, with optional baseline metrics."""

import json
import statistics
import sys
from pathlib import Path


def records(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    meta = next(r for r in rows if r.get("kind") == "shot_meta")
    return {r["doc"]: r for r in rows if "doc" in r}, meta


def equivalent(left, right):
    shared = sorted(set(left) & set(right))
    mismatches = [name for name in shared if any(
        left[name].get(k) != right[name].get(k)
        for k in ("input_sha256", "ok", "reason", "chunk_sha256", "n_chunks", "vector_dim"))]
    missing = sorted(set(left) ^ set(right))
    return {"PASS": bool(shared) and not mismatches and not missing,
            "compared": len(shared), "mismatch_docs": mismatches,
            "missing_docs": missing}


def summarize(root):
    baseline_path = root / "baseline/RUN_REPORT.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    diagnostic = json.loads((root / "diagnostic/RUN_REPORT.json").read_text())
    result = {"scope": "paired deployed services, 32-document waves",
              "diagnostic_only": baseline is None,
              "diagnostic_reps": 1,
              "baseline_comparison_eligible": baseline["comparison_eligible"] if baseline else False,
              "baseline_cross_arm": baseline["cross_arm"] if baseline else None,
              "diagnostic_cross_arm": diagnostic["cross_arm"], "arms": {}}
    raw = {}
    for short, name in (("rr", "rocketride"), ("lg", "langgraph")):
        paths = sorted((root / "baseline" / short).glob("rep*/per_doc.jsonl"))
        pairs = [records(p) for p in paths]
        rows, meta = records(root / "diagnostic" / short / "rep1/per_doc.jsonl")
        raw[short] = pairs[0][0] if pairs else rows
        session = json.loads((root / "diagnostic" / short / "rep1/profiler/session.json").read_text())
        baseline_span = statistics.median(m["span_s"] for _, m in pairs) if pairs else None
        runtime = session.get("stop", {}).get("runtime")
        result["arms"][name] = {
            "baseline": baseline["arms"][name] if baseline else None,
            "baseline_reps": len(pairs),
            "diagnostic": diagnostic["arms"][name],
            "profile_matches_each_baseline": [equivalent(r, rows) for r, _ in pairs],
            "profiler_complete": session.get("complete"),
            "profile_runtime_s": runtime,
            "measured_diagnostic_span_s": meta["span_s"],
            "profile_covers_workload": bool(runtime is not None and runtime >= meta["span_s"] * 0.99),
            "diagnostic_elapsed_vs_baseline_median": meta["span_s"] / baseline_span if baseline_span else None,
            "overhead_caveat": ("one diagnostic replay; exploratory estimate, no confidence interval"
                                if baseline_span else "not estimated: no unprofiled baseline requested"),
        }
    result["cross_arm_all_outcomes"] = equivalent(raw["rr"], raw["lg"])
    result["cross_arm_all_outcomes_source"] = "baseline" if baseline else "diagnostic"
    if not result["cross_arm_all_outcomes"]["PASS"]:
        result["baseline_comparison_eligible"] = False
    (root / "campaign-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"comparison_eligible": result["baseline_comparison_eligible"],
        "cross_arm_mismatches": len(result["cross_arm_all_outcomes"]["mismatch_docs"]),
        "profiles": {k: {f: v[f] for f in ("profiler_complete", "profile_covers_workload",
                    "profile_runtime_s", "diagnostic_elapsed_vs_baseline_median")}
                     for k, v in result["arms"].items()}}, indent=2))


if __name__ == "__main__":
    summarize(Path(sys.argv[1]))
