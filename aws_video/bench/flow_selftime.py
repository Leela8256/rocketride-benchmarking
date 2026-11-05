#!/usr/bin/env python3
"""EXCLUSIVE per-node self time from the engine's own apaevt_flow traces.

Two things bite anyone who reads apaevt_flow naively. Both were learned the
hard way on the PDF arm (aws_pdf, 2026-09-21) and both are worse here.

  (1) DURATIONS NEST. The engine drives the whole downstream chain
      synchronously inside the upstream node's lane call, so on the video pipe
      frame_grabber_1 CONTAINS detect_1 contains preprocessor_1 contains
      embedding_1 contains response_1 -- depth 5. A raw leave-minus-enter total
      is therefore INCLUSIVE and will always indict the outermost node, which
      is the one node that is certainly not the problem. What names the
      expensive node is EXCLUSIVE self time: a frame's duration minus the time
      spent inside the frames nested in it. That is what this module computes.

  (2) LANE CALLS ARE NOT DOCUMENTS. A node receives several enter/leave pairs
      per item (PDF parse_1: ~11 per document). Every count here is `calls`,
      never `documents`, and nothing divides by the document count.

Timing basis: no apaevt_flow event carries a clock, so the CLIENT stamps
arrival. Websocket delivery latency is common-mode on one socket and cancels
in a leave-minus-enter difference; jitter does not. Read the shape, not the
third digit.

Pairing is by (pipe id, component) as the observability doc requires --
"pair enter/leave by identity (component), not stack position" -- with a
per-pipe frame stack so nesting is measurable rather than assumed.

Module use (bench_video.py):
    from flow_selftime import summarize_flow
    summary = summarize_flow(flow_raw, t0_ns, span_s)

CLI use:
    python3 bench/flow_selftime.py <rr_rep_dir> [--lg <lg_rep_dir>] [--json <out.json>]
"""

import bisect
import json
import sys
from pathlib import Path

# The detect pipe's nodes, in lane order (pipe/benchmark_video_detect.pipe):
#   webhook_1 -> frame_grabber_1 -> detect_1 -> preprocessor_1 ->
#   embedding_1 -> response_1
# LangGraph times four of them in-process (arms/langgraph/graph.py:38-70,
# surfaced as rec["lg_timings"] by bench/lg_driver.py:64). Its assemble_node
# is NOT timed, so response_1 has no counterpart -- stated, not silently
# dropped, because an untimed stage is a real asymmetry in any split.
NODE_TO_LG = {
    "frame_grabber_1": "frames_s",
    "detect_1": "detect_s",
    "preprocessor_1": "chunk_s",
    "embedding_1": "embed_s",
    "response_1": None,          # assemble: not timed on the LangGraph arm
}
LG_KEYS = ("frames_s", "detect_s", "chunk_s", "embed_s")

BASIS = ("client arrival timestamps on paired apaevt_flow enter/leave; "
         "durations are EXCLUSIVE self time (nested children subtracted)")


# ---------------------------------------------------------------- pairing ---

def pair_flow(flow_raw):
    """Pair enter/leave into closures carrying inclusive AND exclusive time.

    flow_raw: iterable of (ts_ns, pipe_id, op, component) in arrival order.

    Returns (closures, stats). Each closure is
        {pipe, component, enter_ns, leave_ns, dur_ns, self_ns, implicit}
    where dur_ns is INCLUSIVE and self_ns is EXCLUSIVE of everything nested
    inside it. A frame's duration is folded into its parent's child
    accumulator on close, so the parent's self time is correct no matter how
    deep the chain goes.

    A `leave` is matched against the TOPMOST open frame of the same component
    on that pipe. If that frame is not the innermost one, the frames above it
    never got their own leave: they are closed at the same instant and counted
    in `implicit_close` rather than being dropped or, worse, left to poison
    the parent's self time.
    """
    stacks = {}                  # pipe id -> [[component, enter_ns, child_ns]]
    closures = []
    unmatched_leave = 0
    implicit_close = 0
    max_depth = 0

    def close_down_to(pipe, idx, ts):
        """Pop innermost-first down to and including idx. Returns how many
        frames were closed only because an OUTER leave arrived."""
        st = stacks[pipe]
        n_implicit = 0
        for j in range(len(st) - 1, idx - 1, -1):
            comp, start, child = st.pop()
            dur = ts - start
            if st:
                st[-1][2] += dur      # parent's inclusive child time
            closures.append({
                "pipe": pipe, "component": comp or "<unnamed>",
                "enter_ns": start, "leave_ns": ts,
                "dur_ns": dur, "self_ns": dur - child,
                "implicit": j > idx,
            })
            if j > idx:
                n_implicit += 1
        return n_implicit

    for ts, pipe, op, comp in flow_raw:
        if op == "enter":
            st = stacks.setdefault(pipe, [])
            st.append([comp, ts, 0])
            max_depth = max(max_depth, len(st))
        elif op == "leave":
            st = stacks.get(pipe)
            idx = None
            if st:
                for j in range(len(st) - 1, -1, -1):
                    if st[j][0] == comp:
                        idx = j
                        break
            if idx is None:
                unmatched_leave += 1
                continue
            implicit_close += close_down_to(pipe, idx, ts)
        # begin/end carry no component boundary -- counted by the driver, not
        # used for pairing. Frames still open at the end are reported below,
        # never force-closed: a synthesized duration is a fabricated number.

    stats = {"max_depth": max_depth,
             "unmatched_leave": unmatched_leave,
             "unclosed_enter": sum(len(v) for v in stacks.values()),
             "implicit_close": implicit_close}
    closures.sort(key=lambda c: c["leave_ns"])
    return closures, stats


def _pct(sorted_ns, p):
    return sorted_ns[min(len(sorted_ns) - 1, int(p * len(sorted_ns)))] / 1e6


def summarize_closures(closures, span_s, stats, warmup_dropped=0):
    """Aggregate paired closures into the per-node table. Ranked by SELF."""
    by_comp = {}
    for c in closures:
        e = by_comp.setdefault(c["component"], {"self": [], "incl": 0})
        e["self"].append(c["self_ns"])
        e["incl"] += c["dur_ns"]
    grand = sum(sum(e["self"]) for e in by_comp.values()) / 1e9
    nodes = {}
    for comp, e in sorted(by_comp.items(), key=lambda kv: -sum(kv[1]["self"])):
        durs = sorted(e["self"])
        total = sum(durs) / 1e9
        nodes[comp] = {
            "calls": len(durs),
            "total_s": round(total, 3),
            # Share of summed node SELF time -- the figure to quote. Self
            # times are additive (that is the whole point of subtracting the
            # children), so these do sum to 100.
            "pct_of_node_time": round(100 * total / grand, 2) if grand else None,
            # NOT a fraction of the run: with C items in flight the node
            # totals sum to roughly C x span, so this can exceed 100.
            "pct_of_span": round(100 * total / span_s, 2) if span_s else None,
            "p50_ms": round(_pct(durs, 0.50), 3),
            "p95_ms": round(_pct(durs, 0.95), 3),
            "max_ms": round(durs[-1] / 1e6, 3),
            # Kept so the nesting trap stays visible in the artifact: on a
            # synchronous chain the outermost node's inclusive total is the
            # whole pipeline and means nothing on its own.
            "inclusive_total_s": round(e["incl"] / 1e9, 3),
        }
    return {
        "basis": BASIS,
        "span_s": round(span_s, 3) if span_s else None,
        "self_total_s": round(grand, 3),
        "nodes": nodes,
        "nested": stats["max_depth"] > 1,
        "max_nesting_depth": stats["max_depth"],
        "unmatched_leave": stats["unmatched_leave"],
        "unclosed_enter": stats["unclosed_enter"],
        "implicit_close": stats["implicit_close"],
        "warmup_calls_dropped": warmup_dropped,
    }


def summarize_flow(flow_raw, t0_ns, span_s):
    """What the driver calls: pair, drop warm-up, aggregate.

    Frames that OPENED before the measured span began are warm-up and are
    dropped, so this covers the same window as the throughput number.
    """
    closures, stats = pair_flow(flow_raw)
    measured = [c for c in closures if c["enter_ns"] >= t0_ns]
    return summarize_closures(measured, span_s, stats,
                              warmup_dropped=len(closures) - len(measured))


# ------------------------------------------------------------------- I/O ---

def load_flow_events(rep):
    """flow_events.jsonl -> [(ts_ns, pipe, op, component)] in arrival order."""
    p = Path(rep) / "flow_events.jsonl"
    if not p.exists():
        raise SystemExit(f"no flow_events.jsonl in {rep} -- was the run made "
                         f"with RR_TRACE set?")
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append((d["ts_ns"], d.get("pipe"), d.get("op"), d.get("component")))
    return out


def load_per_doc(rep):
    """per_doc.jsonl -> (document records, shot_meta)."""
    p = Path(rep) / "per_doc.jsonl"
    if not p.exists():
        raise SystemExit(f"no per_doc.jsonl in {rep}")
    recs, meta = [], None
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("kind") == "shot_meta":
            meta = d
        else:
            recs.append(d)
    if meta is None:
        raise SystemExit(f"{p} has no shot_meta line")
    return recs, meta


def measured_t0(recs, meta):
    """The measured span's t0 on the per_doc clock (perf_counter_ns).

    shot_meta records the epoch markers and the mono offset, so t0 is exact;
    the submit-minimum is only a fallback for older reps.
    """
    start = meta.get("measurement_start_epoch_ns")
    off = meta.get("mono_offset_ns")
    if isinstance(start, int) and isinstance(off, int):
        return start - off
    subs = [r["submit_ns"] for r in recs if isinstance(r.get("submit_ns"), int)]
    return min(subs) if subs else 0


# ----------------------------------------------------- per-doc attribution ---

def attribute_to_docs(closures, recs):
    """Attribute each closure's SELF time to the document it ran inside.

    Only sound when the run was sequential: then the [submit_ns,
    completion_ns] windows are disjoint (the driver stamps submit AFTER
    acquiring the semaphore) and every flow event falls inside exactly one of
    them, on the same clock. Attribution is arithmetic, not inference.

    Returns (per_doc, outside) where per_doc maps doc name -> {component: ns}
    and `outside` is the self time that landed in the gaps between documents
    (driver bookkeeping, connection turnaround) and belongs to no document.
    """
    windows = sorted(((r["submit_ns"], r["completion_ns"], r["doc"])
                      for r in recs
                      if isinstance(r.get("submit_ns"), int)
                      and isinstance(r.get("completion_ns"), int)),
                     key=lambda w: w[0])
    starts = [w[0] for w in windows]
    per_doc = {w[2]: {} for w in windows}
    outside = {}
    for c in closures:
        i = bisect.bisect_right(starts, c["leave_ns"]) - 1
        tgt = None
        if i >= 0 and c["leave_ns"] <= windows[i][1]:
            tgt = per_doc[windows[i][2]]
        else:
            tgt = outside
        tgt[c["component"]] = tgt.get(c["component"], 0) + c["self_ns"]
    return per_doc, outside


# --------------------------------------------------------------- printing ---

def print_nodes(summary, prefix=""):
    n = summary["nodes"]
    if not n:
        print(f"{prefix}no enter/leave pairs in the measured window")
        return
    print(f"{prefix}{'node':<24}{'calls':>8}{'self s':>10}{'% self':>8}"
          f"{'p50 ms':>10}{'p95 ms':>10}{'max ms':>11}{'incl s':>11}")
    for nm, v in n.items():
        print(f"{prefix}{nm:<24}{v['calls']:>8}{v['total_s']:>10.1f}"
              f"{(v['pct_of_node_time'] or 0):>8.1f}{v['p50_ms']:>10.1f}"
              f"{v['p95_ms']:>10.1f}{v['max_ms']:>11.1f}"
              f"{v['inclusive_total_s']:>11.1f}")
    print(f"{prefix}{'TOTAL (self)':<24}{'':>8}{summary['self_total_s']:>10.1f}")
    print(f"{prefix}nested={summary['nested']} "
          f"max_depth={summary['max_nesting_depth']} "
          f"unmatched_leave={summary['unmatched_leave']} "
          f"unclosed_enter={summary['unclosed_enter']} "
          f"implicit_close={summary['implicit_close']} "
          f"warmup_calls_dropped={summary['warmup_calls_dropped']}")
    if summary["nested"]:
        print(f"{prefix}NOTE: the chain is nested {summary['max_nesting_depth']} "
              f"deep -- 'incl s' for the outermost node is the WHOLE pipeline "
              f"and must never be quoted as that node's cost.")


def _doc_label(name, w=30):
    return name if len(name) <= w else name[:w - 3] + "..."


def print_per_doc(per_doc, outside, recs, order):
    by_name = {r["doc"]: r for r in recs}
    rows = []
    for doc, comps in per_doc.items():
        r = by_name.get(doc, {})
        wall = ((r.get("completion_ns", 0) - r.get("submit_ns", 0)) / 1e9
                if r.get("completion_ns") is not None else 0.0)
        attributed = sum(comps.values()) / 1e9
        rows.append((doc, wall, r.get("n_chunks"), comps, wall - attributed))
    rows.sort(key=lambda t: -t[1])

    head = f"{'document':<30}{'total s':>10}{'chunks':>8}"
    for nm in order:
        head += f"{nm[:11]:>12}"
    head += f"{'unattrib':>11}"
    print(head)
    for doc, wall, chunks, comps, unattr in rows:
        line = (f"{_doc_label(doc):<30}{wall:>10.2f}"
                f"{(chunks if chunks is not None else -1):>8}")
        for nm in order:
            line += f"{comps.get(nm, 0) / 1e9:>12.2f}"
        line += f"{unattr:>11.2f}"
        print(line)
    if outside:
        line = f"{'(between documents)':<30}{'':>10}{'':>8}"
        for nm in order:
            line += f"{outside.get(nm, 0) / 1e9:>12.2f}"
        print(line + f"{'':>11}")
    print("unattrib = document wall time minus the node self time traced "
          "inside its window: upload, queueing and anything the engine does "
          "outside a traced component.")


def print_comparison(rr_stage_s, lg_stage_s, n_docs, restricted_note):
    print(f"{'stage':<22}{'RocketRide s':>14}{'LangGraph s':>13}{'ratio':>9}"
          f"{'RR s/video':>12}{'LG s/video':>12}")
    for node, lg_key in NODE_TO_LG.items():
        rr = rr_stage_s.get(node)
        if rr is None:
            continue
        if lg_key is None:
            print(f"{node:<22}{rr:>14.2f}{'n/a':>13}{'':>9}"
                  f"{rr / n_docs:>12.2f}{'':>12}   (assemble: not timed on LG)")
            continue
        lg = lg_stage_s.get(lg_key, 0.0)
        ratio = (rr / lg) if lg else None
        print(f"{node:<22}{rr:>14.2f}{lg:>13.2f}"
              + (f"{ratio:>8.2f}x" if ratio else f"{'-':>9}")
              + f"{rr / n_docs:>12.2f}{lg / n_docs:>12.2f}")
    rr_tot = sum(v for k, v in rr_stage_s.items() if NODE_TO_LG.get(k))
    lg_tot = sum(lg_stage_s.get(k, 0.0) for k in LG_KEYS)
    print(f"{'TOTAL (mapped)':<22}{rr_tot:>14.2f}{lg_tot:>13.2f}"
          + (f"{rr_tot / lg_tot:>8.2f}x" if lg_tot else f"{'-':>9}")
          + f"{rr_tot / n_docs:>12.2f}{lg_tot / n_docs:>12.2f}")
    print(restricted_note)
    print("BASIS (do not compare to the third digit): RocketRide numbers are "
          "CLIENT-ARRIVAL wall time between apaevt_flow enter and leave -- "
          "delivery latency is common-mode on one socket and cancels in the "
          "difference, jitter does not. LangGraph numbers are in-process "
          "perf_counter around each node. Comparable in SHAPE, not to the "
          "third digit.")


# -------------------------------------------------------------------- CLI ---

def main(argv):
    rep = None
    lg_rep = None
    json_out = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--lg":
            i += 1
            lg_rep = argv[i]
        elif a == "--json":
            i += 1
            json_out = argv[i]
        elif a.startswith("-"):
            raise SystemExit(__doc__.strip().splitlines()[-1])
        elif rep is None:
            rep = a
        else:
            raise SystemExit(f"unexpected argument {a!r}")
        i += 1
    if rep is None:
        raise SystemExit("usage: flow_selftime.py <rr_rep_dir> "
                         "[--lg <lg_rep_dir>] [--json <out.json>]")

    flow = load_flow_events(rep)
    recs, meta = load_per_doc(rep)
    t0 = measured_t0(recs, meta)
    span_s = meta.get("span_s") or 0
    mode = meta.get("mode")

    closures, stats = pair_flow(flow)
    measured = [c for c in closures if c["enter_ns"] >= t0]
    summary = summarize_closures(measured, span_s, stats,
                                 warmup_dropped=len(closures) - len(measured))

    print(f"== per-node EXCLUSIVE self time :: {rep}")
    print(f"   mode={mode} span={span_s}s docs={len(recs)} "
          f"flow_events={len(flow)} calls_measured={len(measured)}")
    print_nodes(summary)
    order = list(summary["nodes"].keys())

    report = {"rr_rep": str(rep), "mode": mode, "node_timings": summary}

    # ---- per-document attribution (sequential runs only) ----
    per_doc, outside = {}, {}
    if mode == "seq":
        print()
        print("== per-document attribution (sequential run: every flow event "
              "falls inside exactly one document's [submit, completion] "
              "window, on the same clock)")
        per_doc, outside = attribute_to_docs(measured, recs)
        print_per_doc(per_doc, outside, recs, order)
        report["per_document"] = {
            d: {k: round(v / 1e9, 4) for k, v in c.items()}
            for d, c in per_doc.items()}
        report["between_documents_s"] = {k: round(v / 1e9, 4)
                                         for k, v in outside.items()}
    else:
        print()
        print(f"== per-document attribution NOT AVAILABLE for mode={mode!r}: "
              f"documents overlap in time, so a flow event's window is "
              f"ambiguous -- the engine tells us which COMPONENT ran, never "
              f"which document it ran for (apaevt_flow carries no file "
              f"identity). Re-run the cell in seq mode to attribute.")
        report["per_document"] = None

    # ---- LangGraph join ----
    if lg_rep:
        lg_recs, lg_meta = load_per_doc(lg_rep)
        lg_by = {r["doc"]: r for r in lg_recs
                 if r.get("ok") and r.get("lg_timings")}
        rr_ok = {r["doc"] for r in recs if r.get("ok")}
        common = sorted(rr_ok & set(lg_by))
        print()
        print(f"== RocketRide node vs LangGraph stage :: {lg_rep}")
        if not common:
            print("   no document is ok in BOTH runs -- nothing comparable")
            report["comparison"] = None
        else:
            lg_stage = {k: sum(lg_by[d]["lg_timings"].get(k, 0) or 0
                               for d in common) for k in LG_KEYS}
            if mode == "seq":
                rr_stage = {}
                for d in common:
                    for comp, ns in per_doc.get(d, {}).items():
                        rr_stage[comp] = rr_stage.get(comp, 0) + ns
                rr_stage = {k: v / 1e9 for k, v in rr_stage.items()}
                note = (f"   restricted to the {len(common)} documents ok in "
                        f"BOTH runs; RocketRide side is per-document "
                        f"attributed (seq).")
            else:
                rr_stage = {k: v["total_s"] for k, v in summary["nodes"].items()}
                note = (f"   RocketRide side covers ALL {len(recs)} documents "
                        f"of a mode={mode!r} run -- it CANNOT be restricted to "
                        f"the {len(common)} common documents, so this row is "
                        f"only comparable if the two corpora are identical.")
            print_comparison(rr_stage, lg_stage, len(common), note)
            if mode == "seq":
                print()
                print("-- per video --")
                print(f"{'document':<30}" + "".join(
                    f"{n[:11]:>12}" for n in NODE_TO_LG) +
                    "".join(f"{k[:-2] + '(LG)':>12}" for k in LG_KEYS))
                for d in sorted(common,
                                key=lambda d: -sum(per_doc.get(d, {}).values())):
                    line = f"{_doc_label(d):<30}"
                    for n in NODE_TO_LG:
                        line += f"{per_doc.get(d, {}).get(n, 0) / 1e9:>12.2f}"
                    for k in LG_KEYS:
                        line += f"{(lg_by[d]['lg_timings'].get(k) or 0):>12.2f}"
                    print(line)
            report["comparison"] = {
                "lg_rep": str(lg_rep), "n_common_docs": len(common),
                "rr_stage_s": {k: round(v, 4) for k, v in rr_stage.items()},
                "lg_stage_s": {k: round(v, 4) for k, v in lg_stage.items()},
                "restricted": mode == "seq",
            }

    if json_out:
        Path(json_out).write_text(json.dumps(report, indent=1))
        print(f"\nwrote {json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
