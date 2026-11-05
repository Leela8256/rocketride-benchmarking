"""Run-to-run stability across repetitions.

A single run is an anecdote. The rule adopted here: n >= 3 repetitions per
framework per mode, report a central value WITH its variation, and freeze the
threshold BEFORE looking at final results so it cannot be rationalised
afterwards.

    CV = stdev / mean          (coefficient of variation, unitless)

    CV <= CV_STABLE  -> STABLE    — may be quoted as a headline number
    CV >  CV_STABLE  -> UNSTABLE  — must be reported as a range, never a point

Median is the central value (robust to one bad rep); mean is reported too
because CV is defined against it.
"""

import statistics as st
from typing import Any, Dict, List, Optional, Sequence

# FROZEN 2026-08-14, before any multi-rep AWS result was inspected.
CV_STABLE = 0.10
MIN_REPS = 3


def stability(values: Sequence[Optional[float]], name: str = "") -> Dict[str, Any]:
    vals = [v for v in values if isinstance(v, (int, float))]
    n = len(vals)
    if n == 0:
        return {"metric": name, "n": 0, "verdict": "NO_DATA", "PASS": False}
    mean = st.mean(vals)
    # Population stdev for n==1 is 0, which would look artificially perfect;
    # report it as insufficient instead.
    sd = st.stdev(vals) if n > 1 else None
    cv = (sd / mean) if (sd is not None and mean) else None
    verdict = ("INSUFFICIENT_REPS" if n < MIN_REPS
               else "STABLE" if (cv is not None and cv <= CV_STABLE)
               else "UNSTABLE")
    return {
        "metric": name,
        "n": n,
        "values": [round(v, 4) for v in vals],
        "median": round(st.median(vals), 4),
        "mean": round(mean, 4),
        "stdev": round(sd, 4) if sd is not None else None,
        "cv": round(cv, 4) if cv is not None else None,
        "cv_threshold": CV_STABLE,
        "min_reps": MIN_REPS,
        "verdict": verdict,
        # Only a STABLE result with enough reps may be quoted as a point value.
        "PASS": verdict == "STABLE",
        "quotable": ("point value" if verdict == "STABLE"
                     else f"range {min(vals):.4g}-{max(vals):.4g} only"),
    }


def across_reps(reps: List[Dict[str, Any]], paths: Dict[str, Sequence[str]]
                ) -> Dict[str, Any]:
    """Apply stability() to several metrics pulled from a list of rep reports.

    `paths` maps an output name to a key path inside each rep dict, e.g.
        {"docs_per_s": ("m1_throughput", "docs_per_s")}
    A missing path contributes nothing rather than raising: a rep that failed
    to produce a metric must not silently become a zero.
    """
    out = {}
    for name, path in paths.items():
        vals = []
        for rep in reps:
            cur: Any = rep
            for key in path:
                cur = cur.get(key) if isinstance(cur, dict) else None
                if cur is None:
                    break
            vals.append(cur)
        out[name] = stability(vals, name)
    out["all_stable"] = all(v["PASS"] for k, v in out.items()
                            if isinstance(v, dict) and "PASS" in v)
    return out
