"""M1 throughput + M2 latency. See README for formulas and labeling rules."""

from typing import Any, Dict, List, Optional

from metrics.records import by_completion, latency_s, ok_records


def perf_window(rows: List[Dict], warm_n: int = 0) -> Dict[str, Any]:
    """Slice a run into (warmup, window) by COMPLETION order.

    warm_n=0 -> whole run is the window. warm_n=20/25 -> the first N
    completions are excluded from measurement; the window spans from the
    Nth completion to the last.
    """
    done = by_completion(rows)
    if len(done) <= warm_n:
        return {"error": f"{len(done)} completions <= warm_n={warm_n}"}
    if warm_n:
        boundary_ns = done[warm_n - 1]["completion_ns"]
        window = [r for r in done if r["completion_ns"] > boundary_ns]
        span_s = (done[-1]["completion_ns"] - boundary_ns) / 1e9
    else:
        boundary_ns = None
        window = done
        t0 = min(r["submit_ns"] for r in rows if "submit_ns" in r)
        span_s = (done[-1]["completion_ns"] - t0) / 1e9
    return {"window": window, "span_s": span_s, "warm_n": warm_n,
            "boundary_ns": boundary_ns}


def throughput(rows: List[Dict], warm_n: int = 0) -> Dict[str, Any]:
    """M1. Reports docs/s AND chunks/s.

    A document is not a fixed unit of work: two frameworks can process the
    same PDFs and produce different chunk counts through different extraction
    or splitting behaviour. docs_per_s must therefore never be quoted alone --
    always publish chunks_per_s beside it.

    window_t0_ns/window_t1_ns are returned so the resource sampler can be
    sliced to the IDENTICAL window; throughput and CPU accounting must cover
    the same span or the cost-per-work numbers are meaningless.
    """
    w = perf_window(rows, warm_n)
    if "error" in w:
        return w
    ok = ok_records(w["window"])
    chunks = sum(r.get("n_chunks") or 0 for r in ok)
    span = w["span_s"]
    t1 = max((r["completion_ns"] for r in w["window"]), default=None)
    t0 = (w["boundary_ns"] if w["boundary_ns"] is not None
          else min((r["submit_ns"] for r in rows if "submit_ns" in r), default=None))
    return {
        "successful_in_window": len(ok),
        "window_docs": len(w["window"]),
        "window_span_s": round(span, 3),
        "successful_chunks": chunks,
        "docs_per_s": round(len(ok) / span, 4) if span > 0 else None,
        "chunks_per_s": round(chunks / span, 4) if span > 0 else None,
        "warm_n": warm_n,
        "window_t0_ns": t0,
        "window_t1_ns": t1,
    }


def achieved_concurrency(rows: List[Dict], warm_n: int = 0) -> Dict[str, Any]:
    """Concurrency actually ACHIEVED, derived from per-doc intervals.

    Offered concurrency is what the client asked for; configured concurrency
    is what the framework was told to run; this is what actually happened.
    They are three different numbers and must never be conflated -- asking for
    C=8 does not prove eight requests were ever simultaneously in flight.

    Computed by sweeping [submit_ns, completion_ns) intervals: +1 at each
    submit, -1 at each completion, tracking the running maximum. Time-weighted
    mean is the honest average, since a spike lasting 1 ms should not count
    the same as a plateau lasting 10 s.
    """
    w = perf_window(rows, warm_n)
    if "error" in w:
        return w
    spans = [(r["submit_ns"], r["completion_ns"]) for r in w["window"]
             if "submit_ns" in r and "completion_ns" in r]
    if not spans:
        return {"error": "no intervals"}
    events = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
    cur = peak = 0
    area = 0.0
    prev_ns = events[0][0]
    for ns, delta in events:
        area += cur * (ns - prev_ns)
        prev_ns = ns
        cur += delta
        peak = max(peak, cur)
    total_ns = events[-1][0] - events[0][0]
    return {
        "peak_achieved": peak,
        "mean_achieved_time_weighted": round(area / total_ns, 3) if total_ns else None,
        "n_intervals": len(spans),
        "warm_n": warm_n,
    }


def latency(rows: List[Dict], warm_n: int = 0,
            mode: str = "closed-loop") -> Dict[str, Any]:
    """mode is a LABEL: 'closed-loop' => true service latency;
    'open-loop-blast' => batch-position latency (includes queue wait)."""
    w = perf_window(rows, warm_n)
    if "error" in w:
        return w
    lats = sorted(latency_s(r) for r in ok_records(w["window"]))
    if not lats:
        return {"error": "no successful docs in window"}
    q = lambda p: round(lats[min(int(len(lats) * p), len(lats) - 1)], 3)
    return {
        "n": len(lats),
        "p50": q(0.50), "p90": q(0.90), "p95": q(0.95), "p99": q(0.99),
        "max": round(lats[-1], 3),
        "mean": round(sum(lats) / len(lats), 3),
        "label": ("true service latency" if mode == "closed-loop"
                  else "batch-position latency — includes queue wait"),
        "mode": mode, "warm_n": warm_n,
    }


def ttfr(rows: List[Dict]) -> Optional[float]:
    """Time to first result: first completion - first submission."""
    done = by_completion(ok_records(rows))
    if not done:
        return None
    t0 = min(r["submit_ns"] for r in rows if "submit_ns" in r)
    return round((done[0]["completion_ns"] - t0) / 1e9, 3)
