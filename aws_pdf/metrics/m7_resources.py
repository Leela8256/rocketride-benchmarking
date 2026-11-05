"""M7 — resource footprint + efficiency, from sampler JSONL streams."""

import json
import statistics as st
from pathlib import Path
from typing import Any, Dict, Optional


def _load(path) -> list:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def container_resources(sampler_path) -> Optional[Dict[str, Any]]:
    """From pdf1k/proc_sampler.py output (cpu_total_s cumulative)."""
    lines = _load(sampler_path)
    if len(lines) < 3:
        return None
    a, b = lines[0], lines[-1]
    span = b["ts"] - a["ts"]
    rss = [l["rss_mb_sum"] for l in lines]
    thr = [l["n_threads"] for l in lines]
    cpu_s = b["cpu_total_s"] - a["cpu_total_s"]
    return {
        "samples": len(lines), "span_s": round(span, 1),
        "cpu_seconds": round(cpu_s, 1),
        "effective_cores": round(cpu_s / span, 2) if span > 0 else None,
        "rss_mb": {"peak": max(rss), "median": st.median(rss),
                   "start": rss[0], "end": rss[-1],
                   "growth": round(rss[-1] - rss[0], 1)},
        "threads": {"peak": max(thr), "median": st.median(thr)},
    }


def native_resources(sampler_path) -> Optional[Dict[str, Any]]:
    """From gate50/host_sampler_native.py output (%CPU instantaneous)."""
    lines = _load(sampler_path)
    if len(lines) < 3:
        return None
    span = lines[-1]["ts"] - lines[0]["ts"]
    rss = [l["rss_mb_sum"] for l in lines]
    thr = [l["threads_sum"] for l in lines]
    cpu = [l["cpu_pct_sum"] for l in lines]
    mean_cores = st.mean(cpu) / 100
    return {
        "samples": len(lines), "span_s": round(span, 1),
        "cpu_seconds": round(mean_cores * span, 1),
        "effective_cores": round(mean_cores, 2),
        "peak_cores": round(max(cpu) / 100, 2),
        "rss_mb": {"peak": max(rss), "median": st.median(rss),
                   "start": rss[0], "end": rss[-1],
                   "growth": round(rss[-1] - rss[0], 1)},
        "threads": {"peak": max(thr), "median": st.median(thr)},
    }


# How far past the window's end the first later sample may lie and still be
# used as the end anchor. Pipeline teardown between the last completion and a
# late sample burns near-zero CPU, so a few minutes is safe; beyond that the
# gap is a different question and the inside sample is used, flagged.
END_ANCHOR_TOLERANCE_S = 300.0


def window(sampler_path, t0_ns: Optional[int], t1_ns: Optional[int],
           mono_offset_ns: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Re-slice a sampler stream to EXACTLY the throughput window.

    Resource cost and throughput must cover the same span, or cost-per-work is
    a ratio of two different things -- setup and warm-up CPU otherwise land in
    the window and inflate it. Anchors on the last sample at or before t0 (the
    cumulative counter's value entering the window), not the first sample
    after it.

    Sampler ts is wall-clock epoch seconds; per-doc timestamps are monotonic
    ns. mono_offset_ns converts: epoch_ns = mono_ns + mono_offset_ns, recorded
    by the driver. Without it, returns None rather than guess.
    """
    if t0_ns is None or t1_ns is None or mono_offset_ns is None:
        return None
    lines = _load(sampler_path)
    if len(lines) < 3:
        return None
    t0 = (t0_ns + mono_offset_ns) / 1e9
    t1 = (t1_ns + mono_offset_ns) / 1e9
    before = [l for l in lines if l["ts"] <= t0]
    inside = [l for l in lines if t0 < l["ts"] <= t1]
    after = [l for l in lines if l["ts"] > t1]
    if not inside:
        return None
    a = before[-1] if before else inside[0]
    # End anchor: the counter's value LEAVING the window -- the first sample
    # at or after t1 when one exists close by, the mirror of the t0 anchor.
    # A sampler that died before t1 (a capped SAMPLE_MAX_S on a rep that ran
    # longer than anyone planned -- the 2026-09-16 b16 cell ran 5 h against
    # a 2 h cap) otherwise truncates CPU to whatever it happened to see and
    # understates cost-per-work silently. Whichever anchor is used, and any
    # uncovered stretch, is DISCLOSED in `coverage`.
    b = inside[-1]
    end_anchor = "last sample inside window"
    if after and (after[0]["ts"] - t1) <= END_ANCHOR_TOLERANCE_S:
        b = after[0]
        end_anchor = f"first sample after window (+{after[0]['ts'] - t1:.1f} s)"
    span = b["ts"] - a["ts"]
    if span <= 0:
        return None
    rss = [l["rss_mb_sum"] for l in inside]
    thr = [l["n_threads"] for l in inside]
    cpu_s = b["cpu_total_s"] - a["cpu_total_s"]
    max_gap = max((y["ts"] - x["ts"] for x, y in zip(inside, inside[1:])),
                  default=0.0)
    uncovered_tail = max(0.0, t1 - inside[-1]["ts"])
    coverage = {
        "window_s": round(t1 - t0, 2),
        "start_anchor": ("last sample at/before window start" if before
                         else "first sample inside window"),
        "end_anchor": end_anchor,
        "uncovered_tail_s": round(uncovered_tail, 1),
        "max_sample_gap_s": round(max_gap, 1),
        "note": "cpu_seconds is exact between the two anchors (cumulative "
                "cgroup counter); rss/thread stats cover sampled instants only",
    }
    if uncovered_tail > 60 or max_gap > 60:
        coverage["WARNING"] = ("sampler did not cover the whole window: "
                               "rss/thread stats are partial"
                               + ("" if b is not inside[-1] else
                                  "; cpu_seconds is TRUNCATED at the last "
                                  "inside sample"))
    return {
        "samples_in_window": len(inside), "span_s": round(span, 2),
        "cpu_seconds": round(cpu_s, 2),
        "effective_cores": round(cpu_s / span, 3),
        "coverage": coverage,
        "rss_mb": {"peak": max(rss), "median": st.median(rss),
                   "start": rss[0], "end": rss[-1],
                   "growth": round(rss[-1] - rss[0], 1)},
        "threads": {"peak": max(thr), "median": st.median(thr),
                    "note": "observation only — threads include torch, "
                            "tokenizers, GC and HTTP runtime; NOT proof of "
                            "parallel document execution"},
    }


def efficiency(successful_docs: Optional[int], cpu_seconds: Optional[float],
               successful_chunks: Optional[int] = None,
               wall_seconds: Optional[float] = None,
               available_cpus: Optional[int] = None,
               arm_cpus: Optional[float] = None) -> Dict[str, Any]:
    """Work-normalised cost. Per-chunk pairs with chunks_per_s: it is the
    honest denominator when two frameworks split the same PDFs differently.

    cpu_utilization answers what throughput alone hides -- 20 docs/s at 80%
    CPU and 19 docs/s at 40% are very different results. It is bounded at 1.0
    by construction; above that means cost was attributed outside the window
    (a real bug this suite has already hit), so it is flagged, never clamped.
    """
    if not cpu_seconds or successful_docs is None:
        return {"error": "missing inputs"}
    out: Dict[str, Any] = {
        "docs_per_cpu_second": round(successful_docs / cpu_seconds, 4),
        "cpu_seconds_per_doc": round(cpu_seconds / successful_docs, 3)
        if successful_docs else None,
    }
    if successful_chunks:
        out["chunks_per_cpu_second"] = round(successful_chunks / cpu_seconds, 4)
        out["cpu_seconds_per_chunk"] = round(cpu_seconds / successful_chunks, 4)
    else:
        out["cpu_seconds_per_chunk"] = None
    # TWO denominators, because they answer different questions and the same
    # run reads very differently under each. An arm capped at 12 on a 32-core
    # box can never exceed 0.375 against the host, which makes the host figure
    # useless as a saturation signal -- so the ARM figure is the headline.
    #
    #   cpu_utilization      = of what this arm was ALLOWED  -> saturation
    #   cpu_utilization_host = of the whole machine          -> capacity used
    #
    # Measured example: LangGraph at 4.013 effective cores is 33.4% of its
    # 12-CPU allocation but only 12.5% of the host. Reporting only the latter
    # would read as "idle" when the arm was a third saturated.
    if wall_seconds:
        denom = arm_cpus or available_cpus
        if denom:
            util = cpu_seconds / (wall_seconds * denom)
            out["cpu_utilization"] = round(util, 4)
            out["cpu_utilization_basis"] = (
                f"arm allocation ({denom} cpus)" if arm_cpus
                else f"host ({denom} cpus) — NO arm cap recorded")
            out["arm_cpus"] = arm_cpus
            # Bounded at 1.0 by physics. Above it, cost was attributed outside
            # the window or outside this arm -- flagged, never clamped.
            out["cpu_utilization_valid"] = util <= 1.0
            if util > 1.0:
                out["cpu_utilization_error"] = (
                    "utilization > 1.0 — CPU attributed outside the measured "
                    "window or outside this arm; the run is INVALID")
        if available_cpus:
            out["cpu_utilization_host"] = round(
                cpu_seconds / (wall_seconds * available_cpus), 4)
            out["host_cpus"] = available_cpus
    return out


def combined_peak_rss(path_a, path_b, t0_ns=None, t1_ns=None,
                      mono_offset_ns=None, tolerance_s: float = 1.0
                      ) -> Optional[Dict[str, Any]]:
    """Peak of (A + B) at the SAME instant, not peak(A) + peak(B).

    An arm made of two containers has one real memory high-water mark: the
    largest simultaneous sum. Adding independent peaks overstates it whenever
    they occur at different times, which for a parse-then-embed pipeline they
    routinely do -- the parser peaks early, the embedder later.

    Samples are matched by wall-clock ts within `tolerance_s`; an A sample with
    no contemporaneous B sample is skipped rather than paired with a distant
    one.
    """
    a, b = _load(path_a), _load(path_b)
    if len(a) < 2 or len(b) < 2:
        return None
    lo = hi = None
    if t0_ns is not None and t1_ns is not None and mono_offset_ns is not None:
        lo = (t0_ns + mono_offset_ns) / 1e9
        hi = (t1_ns + mono_offset_ns) / 1e9
        a = [x for x in a if lo <= x["ts"] <= hi] or a
    b_sorted = sorted(b, key=lambda x: x["ts"])
    ts_b = [x["ts"] for x in b_sorted]
    import bisect
    best = None
    for sa in a:
        i = bisect.bisect_left(ts_b, sa["ts"])
        cands = [j for j in (i - 1, i) if 0 <= j < len(b_sorted)]
        if not cands:
            continue
        j = min(cands, key=lambda k: abs(ts_b[k] - sa["ts"]))
        if abs(ts_b[j] - sa["ts"]) > tolerance_s:
            continue
        total = sa["rss_mb_sum"] + b_sorted[j]["rss_mb_sum"]
        if best is None or total > best["combined_mb"]:
            best = {"combined_mb": round(total, 1), "ts": sa["ts"],
                    "a_mb": sa["rss_mb_sum"], "b_mb": b_sorted[j]["rss_mb_sum"]}
    if best is None:
        return {"error": "no contemporaneous samples within tolerance"}
    best["method"] = "max of simultaneous sum (NOT peak_a + peak_b)"
    best["tolerance_s"] = tolerance_s
    return best
