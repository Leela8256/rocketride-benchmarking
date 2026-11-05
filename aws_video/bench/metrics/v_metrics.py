"""V1–V5 — the numbers (METRICS.md). All work-normalized where possible;
latency semantics are mode-labeled and never blended (MLPerf discipline).

Inputs are what a run directory already contains: the per-doc records,
shot_meta, the cgroup sampler CSV, and optionally progress.jsonl.
"""

DEFAULT_USD_PER_HOUR = 1.428      # c7i.8xlarge on-demand us-east-1
DEFAULT_ALLOCATED_CORES = 32


def _pct(sorted_vals, p):
    """Nearest-rank percentile — deterministic, no interpolation (the
    haystack-suite convention, adopted 2026-08-21)."""
    if not sorted_vals:
        return None
    import math
    k = max(1, math.ceil(len(sorted_vals) * p / 100)) - 1
    return sorted_vals[min(k, len(sorted_vals) - 1)]


def _footage_s(meta, ok):
    """Footage denominator, in authority order (2026-08-23): probed
    video-stream duration; the legacy measured_audio_s key (which new
    drivers alias to the probed value); per-record probed; per-record
    source metadata last (old AMI artifacts)."""
    return (meta.get("measured_video_s") or meta.get("measured_audio_s")
            or sum(r.get("video_duration_s") or r.get("duration_s") or 0
                   for r in ok))


def v1_throughput(records, meta):
    ok = [r for r in records if r.get("ok")]
    span = meta.get("span_s") or 0
    audio_s = _footage_s(meta, ok)
    frames = sum(r.get("n_frames_est") or 0 for r in ok)
    chunks = sum(r.get("n_chunks") or 0 for r in ok)
    if not span:
        return {"error": "no span"}
    x_rt = audio_s / span if audio_s else None
    return {
        "x_realtime": round(x_rt, 2) if x_rt else None,
        "videos_per_s": round(len(ok) / span, 4),
        "chunks_per_s": round(chunks / span, 3),
        "frames_per_s": round(frames / span, 3) if frames else None,
        "chunks_per_video": round(chunks / len(ok), 1) if ok else None,
        "frames_per_video": round(frames / len(ok), 1) if ok and frames else None,
        "realtime_streams_sustainable": round(x_rt, 1) if x_rt else None,
        "video_seconds": round(audio_s, 1) if audio_s else None,
        "footage_hours": round(audio_s / 3600, 2),
        "span_s": span,
    }


def v2_latency(records, meta, progress=None):
    mode = meta.get("mode", "?")
    ok = [r for r in records if r.get("ok")
          and r.get("submit_ns") and r.get("completion_ns")]
    out = {"mode": mode}
    if mode in ("seq",) or mode.startswith("c"):
        lats = sorted((r["completion_ns"] - r["submit_ns"]) / 1e9 for r in ok)
        per_min = sorted(
            (r["completion_ns"] - r["submit_ns"]) / 1e9 / (r["duration_s"] / 60)
            for r in ok if r.get("duration_s"))
        failed = [r["doc"] for r in records if not r.get("ok")]
        out.update({
            "service_latency_s": {"p50": round(_pct(lats, 50), 2),
                                  "p95": round(_pct(lats, 95), 2),
                                  "p99": round(_pct(lats, 99), 2)} if lats else None,
            "latency_s_per_footage_min": round(_pct(per_min, 50), 3) if per_min else None,
            "failed_items": len(failed),
            "time_to_first_result_s": (round(min(
                (r["completion_ns"] - r["submit_ns"]) / 1e9 for r in ok), 1)
                if ok else None),
            "time_to_first_result_basis": "first completed request (per-item mode)",
        })
    else:  # blast / batch: batch-position semantics, never service latency
        out["note"] = ("batch span is exact; per-doc completion is batch-"
                       "position (includes queue wait) — no service-latency claims")
        out["batch_span_s"] = meta.get("span_s")
        if progress:
            ts = sorted(p["t_rel_s"] for p in progress
                        if p.get("action") in ("complete", "completed"))
            if ts:
                out["time_to_first_result_s"] = round(ts[0], 1)
                out["time_to_first_result_basis"] = (
                    "first client-observed completion event within the batch — "
                    "NOT comparable to an atomic batch API's first result "
                    "without this basis string")
                out["completion_curve_s"] = {"p50": round(_pct(ts, 50), 1),
                                             "p90": round(_pct(ts, 90), 1),
                                             "last": round(ts[-1], 1)}
    return out


def _interp_counter(rows, t):
    """Linear-interpolate a cumulative counter (col 1) at epoch second t.
    Returns (value, clamped) — clamped=True when t falls outside the
    sampled range and the nearest edge value is used instead."""
    if t <= rows[0][0]:
        return rows[0][1], t < rows[0][0]
    if t >= rows[-1][0]:
        return rows[-1][1], t > rows[-1][0]
    for a, b in zip(rows, rows[1:]):
        if a[0] <= t <= b[0]:
            if b[0] == a[0]:
                return a[1], False
            f = (t - a[0]) / (b[0] - a[0])
            return a[1] + f * (b[1] - a[1]), False
    return rows[-1][1], True


def cpu_from_sampler(sampler_rows, window_epoch_s=None):
    """sampler_rows: (epoch_ts, cpu_usage_usec, mem_current[, pids, anon]).
    Cumulative counters make totals exact over the sampled window.

    window_epoch_s=(start, end): driver measurement markers — CPU is
    interpolated at the boundaries (±one 15 s sampling interval precision)
    so warm-up, model load and driver startup are EXCLUDED. Without
    markers the full sampler span is used and the basis says so."""
    rows = sorted(r for r in sampler_rows if len(r) >= 3)
    if len(rows) < 2:
        return None
    if window_epoch_s:
        t_start, t_end = window_epoch_s
        c0, cl0 = _interp_counter(rows, t_start)
        c1, cl1 = _interp_counter(rows, t_end)
        dt = t_end - t_start
        cpu_s = (c1 - c0) / 1e6
        gauge_rows = [r for r in rows if t_start <= r[0] <= t_end] or rows
        basis = ("driver measurement markers, sampler interpolated at "
                 "boundaries (±15 s)"
                 + ("; marker outside sampled range — clamped" if cl0 or cl1
                    else ""))
    else:
        dt = rows[-1][0] - rows[0][0]
        cpu_s = (rows[-1][1] - rows[0][1]) / 1e6
        gauge_rows = rows
        basis = ("full sampler span — INCLUDES warm-up and driver startup; "
                 "CPU figures are approximate for measured-interval claims")
    if dt <= 0:
        return None
    mem_max = max(r[2] for r in gauge_rows)
    if mem_max > 500e9:      # > box RAM: a corrupted sampler column, not a fact
        mem_max = None
    out = {"window_s": round(dt, 1), "cpu_s": round(cpu_s, 1),
           "effective_cores": round(cpu_s / dt, 2),
           "mem_current_max_bytes": mem_max,
           "window_basis": basis}
    # Optional 4th/5th columns (pids.current, anon bytes) — newer samplers.
    pids = [r[3] for r in gauge_rows if len(r) >= 4]
    if pids:
        out["threads_activated"] = max(pids) - pids[0]
    anon = [r[4] for r in gauge_rows if len(r) >= 5]
    if anon:
        # cgroup memory.stat anon — NOT process RSS (honest-name review)
        out["cgroup_anon_peak_bytes"] = max(anon)
    return out


def v3_efficiency(records, meta, cpu, allocated_cores=DEFAULT_ALLOCATED_CORES):
    if not cpu:
        return {"error": "no sampler data — cpu_s not measurable"}
    ok = [r for r in records if r.get("ok")]
    audio_min = _footage_s(meta, ok) / 60
    frames = sum(r.get("n_frames_est") or 0 for r in ok)
    dets = sum(r.get("n_detections") or 0 for r in ok)
    chunks = sum(r.get("n_chunks") or 0 for r in ok)
    c = cpu["cpu_s"]
    return {
        "cpu_s_per_video": round(c / len(ok), 1) if ok else None,
        "cpu_s_per_footage_min": round(c / audio_min, 2) if audio_min else None,
        "cpu_s_per_frame": round(c / frames, 3) if frames else None,
        "cpu_s_per_detection": round(c / dets, 4) if dets else None,
        "cpu_s_per_chunk": round(c / chunks, 3) if chunks else None,
        "effective_cores": cpu["effective_cores"],
        "achieved_parallelism": cpu["effective_cores"],   # haystack-suite name
        "threads_activated": cpu.get("threads_activated"),
        "allocated_cores": allocated_cores,
        "scaling_efficiency": round(cpu["effective_cores"] / allocated_cores, 3),
        "cpu_window_basis": cpu.get("window_basis"),
        "note": "utilization is against the ARM'S ALLOCATION, averaged over "
                "the CPU window (see cpu_window_basis)",
    }


def v4_resources(records, meta, cpu):
    ok = [r for r in records if r.get("ok")]
    out = {
        "peak_mem_bytes": cpu["mem_current_max_bytes"] if cpu else None,
        "peak_mem_note": "cgroup memory.current max — includes page cache",
        "cold_to_ready_s": meta.get("warm_s"),
    }
    # LangGraph decomposes per node; RocketRide is a black box — the
    # asymmetry itself is reported (METRICS.md V4).
    lg = [r.get("lg_timings") for r in ok if r.get("lg_timings")]
    if lg:
        keys = ("frames_s", "detect_s", "chunk_s", "embed_s")
        tot = {k: sum(t.get(k, 0) for t in lg) for k in keys}
        alls = sum(tot.values()) or 1
        out["lg_stage_split"] = {k: f"{100 * v / alls:.0f}%" for k, v in tot.items()}
        e2e = sum(t.get("total_s", 0) for t in lg)
        out["lg_framework_overhead_s"] = round(e2e - alls, 2) if e2e else None
    else:
        out["stage_split"] = "not decomposable (engine is a black box)"
    return out


def v5_cost(v1, usd_per_hour=DEFAULT_USD_PER_HOUR):
    x = v1.get("x_realtime")
    if not x:
        return {"error": "no x_realtime"}
    # Capacity from the MEASURED corpus (film-metrics review 2026-08-23):
    # videos/day = measured completion rate held for 24 h. The old 30-min
    # normalization survives under its own explicit name.
    vps = v1.get("videos_per_s")
    mean_min = None
    if vps and v1.get("span_s") and v1.get("video_seconds"):
        n_ok = vps * v1["span_s"]
        if n_ok:
            mean_min = round(v1["video_seconds"] / n_ok / 60, 1)
    return {
        "usd_per_1k_footage_hours": round(usd_per_hour / x * 1000, 2),
        "videos_per_day_per_box": int(vps * 86400) if vps else None,
        "mean_measured_video_min": mean_min,
        "equivalent_30min_videos_per_day": int(x * 24 * 2),
        "assumes": f"${usd_per_hour}/h instance; videos_per_day at the "
                   f"measured mean duration ({mean_min} min), not 30-min "
                   f"normalized",
    }


def cross_mode(runs_by_mode, configured_concurrency=None):
    """Same arm, different modes: speedup and parallel efficiency
    (haystack-suite §4). runs_by_mode: {mode: v1_block}. Ratio of ratios —
    each side normalized against itself."""
    seqs = [m for m in runs_by_mode if m == "seq"]
    pars = [m for m in runs_by_mode if m != "seq"]
    if not seqs or not pars:
        return None
    base = runs_by_mode[seqs[0]].get("chunks_per_s")
    out = {}
    for m in pars:
        top = runs_by_mode[m].get("chunks_per_s")
        if base and top:
            sp = round(top / base, 2)
            out[f"speedup_{m}_over_seq"] = sp
            if configured_concurrency:
                out[f"parallel_efficiency_{m}"] = round(
                    sp / configured_concurrency, 3)
    out["note"] = ("parallel_efficiency meaningful only when docs >= "
                   "concurrency")
    return out or None


def coverage(blocks, exemptions=()):
    """The haystack-suite coverage gate: a null metric must be a named
    exemption, or the run fails coverage — 'we did not check' must never
    read as 'it passed'."""
    nulls = []
    for block_name, block in blocks.items():
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            if v is None and not any(e in f"{block_name}.{k}" for e in exemptions):
                nulls.append(f"{block_name}.{k}")
    status = "FAIL" if nulls else "PASS"
    return {"gate": "metric_coverage", "status": status,
            "detail": ("all metrics non-null or exempt" if not nulls else
                       f"null without exemption: {nulls[:8]}")}
