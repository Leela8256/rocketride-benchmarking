"""Run report — per-rep metrics, across-rep stability, cross-arm parity.

Mode-aware. `blast` and `c<N>` are different experiments and are labelled as
such: closed-loop yields SERVICE latency, blast yields BATCH-POSITION latency
(queue wait included). They answer different questions and are never compared.

Nothing is computed here that metrics/ can compute. This file decides WHAT to
ask and WHETHER the run passes; the formulas live in the library.

  python3 bench/report.py <run_dir>
Expects <run_dir>/<arm>/rep<N>/per_doc.jsonl.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # aws_pdf/
sys.path.insert(0, str(ROOT))

from metrics.m0_correctness import (census, cross_arm, determinism,
                                    gate_verdict, input_integrity,
                                    self_duplication, structure)
from metrics.m1_m2_perf import achieved_concurrency, latency, throughput, ttfr
from metrics.m7_resources import (combined_peak_rss, container_resources,
                                  efficiency, window)
from metrics.provenance import check as prov_check
from metrics.records import load_records, ok_records
from metrics.stability import across_reps

# Warm-up is excluded by the DRIVER (real docs, timed separately), so no
# further window slicing is applied here in either mode.
WARM_N = 0
EXPECTED_EMPTY = {"000164.pdf"}

# Text-free (scanned, no text layer) PDFs are a property of a heterogeneous
# corpus, not of either framework -- both arms fail the IDENTICAL set in every
# run. "report" names and counts them without failing the gate; "fail" (the
# default) tolerates only the docs named in EXPECTED_EMPTY.
#
# CAVEAT recorded with the run: both arms extract with Tika, so "both arms
# agree it is empty" is NOT independent evidence. A Tika defect would look
# exactly like a text-free corpus. Establishing that list with a non-Tika
# probe is outstanding; until then this flag is for performance runs, not for
# publishing a correctness claim.
EMPTY_POLICY = os.environ.get("CENSUS_EMPTY_POLICY", "fail")
ARMS = {"lg": "langgraph", "rr": "rocketride"}


def load_manifest_sha(run: Path) -> dict:
    """corpus.sha256 is `<sha>  <name>` per line — the canonical inputs."""
    f = run / "corpus.sha256"
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[Path(parts[-1]).name] = parts[0]
    return out


def rep_report(d: Path, arm: str, cpus, arm_cpus, mode: str,
               manifest_sha: dict) -> dict:
    f = d / "per_doc.jsonl"
    if not f.exists():
        return {"error": f"missing {f}"}
    rows, meta, _ = load_records(f)
    manifest = json.loads((d / "manifest.json").read_text())
    meta = meta or {}
    # The DRIVER records what it actually ran. In native_saturation the two
    # arms run different modes, so a single run-level value would mislabel one
    # of them -- and latency semantics depend on this.
    mode = meta.get("mode") or mode
    # seq IS a closed loop -- one request in flight, completion observed per
    # document. Its latency is service latency; only blast (whole backlog
    # submitted at once) measures queue position.
    closed_loop = mode == "seq" or (mode.startswith("c") and mode[1:].isdigit())
    # A batch response carries no per-document timestamps, so the driver marks
    # what clock each record used. Latency is publishable only when the times
    # were CLIENT-OBSERVED; values reconstructed from the engine's own
    # upload_time are withheld. Judged over all records, not just the first,
    # since a failed record may carry no marker at all.
    sources = {str(r.get("timing_source", "")) for r in rows}
    client_observed = any("client-observed" in x for x in sources)
    derived = any("derived" in x for x in sources)

    rep = {"rep_dir": d.name, "mode": mode, "meta": meta}
    rep["census"] = census(rows, offered=manifest["n"],
                           expected_docs=set(manifest["docs"]),
                           expected_empty=EXPECTED_EMPTY,
                           empty_policy=EMPTY_POLICY)
    rep["structure"] = structure(rows, arm=arm, expected_empty=EXPECTED_EMPTY)
    rep["input_integrity"] = input_integrity(rows, manifest_sha)
    # Permanent gate: an arm that emits its document list twice passes every
    # per-document check while doing twice the work.
    rep["self_duplication"] = self_duplication(rows)
    rep["m0_PASS_partial"] = gate_verdict(rep["census"], rep["structure"],
                                          rep["input_integrity"],
                                          rep["self_duplication"])

    t = throughput(rows, warm_n=WARM_N)
    start, end = meta.get("measurement_t0_ns"), meta.get("measurement_t1_ns")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        span = (end - start) / 1e9
        t.update({"window_t0_ns": start, "window_t1_ns": end,
                  "window_span_s": span,
                  "docs_per_s": t.get("successful_in_window", 0) / span,
                  "chunks_per_s": t.get("successful_chunks", 0) / span,
                  "span_source": "MEASURED driver workload window"})
    # In batch mode the driver's own measured makespan is authoritative,
    # whichever clock the per-document records used.
    elif (derived or (not closed_loop and client_observed)) and meta.get("span_s"):
        span = float(meta["span_s"])
        ok_n = t.get("successful_in_window")
        chunks = t.get("successful_chunks")
        t.update({
            "window_span_s": round(span, 3),
            "docs_per_s": round(ok_n / span, 4) if span else None,
            "chunks_per_s": round(chunks / span, 4) if span else None,
            "span_source": "MEASURED batch makespan (shot_meta.span_s)",
        })
    rep["m1_throughput"] = t
    rep["ttfr_s"] = ttfr(rows)
    rep["timing_source"] = sorted(x for x in sources if x)

    if derived:
        # Withheld rather than published: percentiles over SDK-derived
        # durations are not client-observed latency, and an interval sweep
        # over them reconstructs the offered batch shape rather than proving
        # server-side execution concurrency.
        why = ("UNAVAILABLE — batch mode returns no per-document timestamps; "
               "derived values would not be client-observed")
        rep["m2_latency"] = {"unavailable": why}
        rep["concurrency"] = {"offered": meta.get("offered_concurrency"),
                              "achieved_peak": None,
                              "achieved_unavailable": why}
    else:
        lat = latency(rows, warm_n=WARM_N,
                      mode="closed-loop" if closed_loop else "open-loop-blast")
        lat["label"] = ("SERVICE LATENCY" if closed_loop
                        else "BATCH-POSITION LATENCY — includes queue wait")
        rep["m2_latency"] = lat
        ac = achieved_concurrency(rows, warm_n=WARM_N)
        rep["concurrency"] = {
            "offered": meta.get("offered_concurrency"),
            "configured_note": meta.get("configured_concurrency_note"),
            "achieved_peak": ac.get("peak_achieved"),
            "achieved_mean_time_weighted": ac.get("mean_achieved_time_weighted"),
            "note": "offered != configured != achieved; never treat as equal",
        }

    sampler = d / "sampler.jsonl"
    if sampler.exists():
        w = window(sampler, t.get("window_t0_ns"), t.get("window_t1_ns"),
                   meta.get("mono_offset_ns"))
        rep["m7_resources"] = w or container_resources(sampler)
        rep["m7_window"] = ("sliced to throughput window" if w else
                            "WHOLE STREAM — not window-matched")
        res = rep.get("m7_resources") or {}
        if res:
            rep["m7_efficiency"] = efficiency(
                t.get("successful_in_window"), res.get("cpu_seconds"),
                t.get("successful_chunks"), t.get("window_span_s"), cpus,
                arm_cpus)

    tika = d / "sampler_tika.jsonl"
    if tika.exists():
        # LangGraph PARSES in the sidecar, a different cgroup. RocketRide is
        # charged for its own embedded Tika, so omitting this compares a
        # parsing framework against a non-parsing one.
        tk = window(tika, t.get("window_t0_ns"), t.get("window_t1_ns"),
                    meta.get("mono_offset_ns")) or container_resources(tika)
        rep["m7_resources_tika_sidecar"] = tk
        svc = rep.get("m7_resources") or {}
        if tk and svc:
            total_cpu = round((svc.get("cpu_seconds") or 0)
                              + (tk.get("cpu_seconds") or 0), 2)
            span = svc.get("span_s") or t.get("window_span_s")
            combined = combined_peak_rss(
                sampler, tika, t.get("window_t0_ns"), t.get("window_t1_ns"),
                meta.get("mono_offset_ns"))
            rep["m7_arm_total"] = {
                "cpu_seconds": total_cpu,
                "effective_cores": round(total_cpu / span, 3) if span else None,
                "components": {"service": svc.get("cpu_seconds"),
                               "tika_sidecar": tk.get("cpu_seconds")},
                "peak_rss_mb_contemporaneous": combined,
            }
            rep["m7_efficiency_service_only"] = rep.get("m7_efficiency")
            rep["m7_efficiency"] = efficiency(
                t.get("successful_in_window"), total_cpu,
                t.get("successful_chunks"), t.get("window_span_s"), cpus,
                arm_cpus)
            rep["m7_note"] = ("ARM TOTAL (langgraph + tika) feeds efficiency; "
                              "peak RSS is the max SIMULTANEOUS sum, not "
                              "peak(svc) + peak(tika)")
    return rep


def arm_report(run: Path, arm: str, cpus, arm_cpus, mode: str,
               manifest_sha: dict, want_reps: int) -> dict:
    reps = sorted((run / arm).glob("rep*"))
    rr = [rep_report(d, arm, cpus, arm_cpus, mode, manifest_sha) for d in reps]
    good = [r for r in rr if not r.get("error")]

    # EVERY rep against rep1, not just rep2 — rep3 was previously free to
    # differ without failing anything.
    dets, det_pass = {}, None
    if len(reps) >= 2:
        base, _, _ = load_records(reps[0] / "per_doc.jsonl")
        for other in reps[1:]:
            rows_b, _, _ = load_records(other / "per_doc.jsonl")
            dets[f"rep1_vs_{other.name}"] = determinism(base, rows_b)
        det_pass = all(d.get("PASS") is True for d in dets.values())

    rep_count_ok = (want_reps is None) or (len(reps) == want_reps)
    return {
        "reps": rr,
        "rep_count": len(reps),
        "rep_count_expected": want_reps,
        "rep_count_ok": rep_count_ok,
        "determinism": dets or None,
        "determinism_PASS": det_pass,
        # Fail-closed: fewer than 2 reps means determinism is UNPROVEN.
        "m0_PASS": bool(good) and rep_count_ok
                   and all(r.get("m0_PASS_partial") for r in good)
                   and det_pass is True,
        "stability": across_reps(good, {
            "docs_per_s": ("m1_throughput", "docs_per_s"),
            "chunks_per_s": ("m1_throughput", "chunks_per_s"),
            "p50_s": ("m2_latency", "p50"),
            "p95_s": ("m2_latency", "p95"),
            "effective_cores": ("m7_resources", "effective_cores"),
            "cpu_utilization": ("m7_efficiency", "cpu_utilization"),
            "cpu_s_per_chunk": ("m7_efficiency", "cpu_seconds_per_chunk"),
            "peak_rss_mb": ("m7_resources", "rss_mb", "peak"),
        }),
    }


def main():
    global EMPTY_POLICY
    run = Path(sys.argv[1])
    env = {}
    envf = run / "environment.txt"
    if envf.exists():
        env = dict(l.split("=", 1) for l in envf.read_text().splitlines()
                   if "=" in l)
    EMPTY_POLICY = env.get("census_empty_policy", EMPTY_POLICY)
    cpus = int(env.get("nproc") or 0) or None
    arm_cpus = float(env.get("arm_cpus") or 0) or None
    mode = env.get("mode") or "blast"
    want_reps = int(env.get("reps") or 0) or None
    manifest_sha = load_manifest_sha(run)

    captures = list(run.glob("*/rep*/profiler/session.json"))
    out = {"run_dir": str(run), "mode": mode, "environment": env, "arms": {},
           "unprofiled": not captures,
           "profiler_captures": [str(p.relative_to(run)) for p in captures]}
    for arm, name in ARMS.items():
        if not (run / arm).exists():
            continue
        out["arms"][name] = arm_report(run, arm, cpus, arm_cpus, mode,
                                       manifest_sha, want_reps)
        out["arms"][name]["mode"] = (
            (out["arms"][name]["reps"][0] or {}).get("mode") or mode)

    # ---- determinism by concurrency-invariance -------------------------
    # With one rep, rep-to-rep determinism is unprovable and the gate fails
    # closed. But if bench/cross_mode_determinism.py has compared this run
    # against a run at a DIFFERENT concurrency (c128 vs seq) and the shared
    # docs were byte-identical, that is accepted as the determinism evidence:
    # it rules out the racing-output class directly. The basis is recorded --
    # invariance across concurrency is not the same claim as repeatability
    # across runs, and a reader must see which one was proven. A FAILED
    # invariance check fails the arm outright, evidence beats absence.
    cmd_f = run / "CROSS_MODE_DETERMINISM.json"
    if cmd_f.exists():
        cmd = json.loads(cmd_f.read_text())
        for name, a in out["arms"].items():
            v = (cmd.get("arms") or {}).get(name) or {}
            if v.get("PASS") is True and a.get("determinism_PASS") is None:
                a["determinism_PASS"] = True
                a["determinism"] = {
                    "basis": "concurrency-invariance "
                             f"({v.get('mode_a')} vs {v.get('mode_b')}, "
                             f"{v.get('identical')}/{v.get('compared')} identical)",
                    "cross_mode": v}
                good = [r for r in a["reps"] if not r.get("error")]
                a["m0_PASS"] = (bool(good) and a.get("rep_count_ok")
                                and all(r.get("m0_PASS_partial") for r in good))
            elif v.get("PASS") is False and (v.get("compared") or 0) > 0:
                # Genuine mismatches on shared docs. compared == 0 is NO
                # evidence (disjoint corpora), never a failure verdict.
                a["determinism_PASS"] = False
                a["determinism"] = {"basis": "concurrency-invariance FAILED",
                                    "cross_mode": v}
                a["m0_PASS"] = False

    # ---- cross-arm, EVERY rep ------------------------------------------
    matched = (env.get("lg_extractor") == "tika")
    per_rep, n = {}, 0
    while True:
        n += 1
        lg = run / "lg" / f"rep{n}" / "per_doc.jsonl"
        rr = run / "rr" / f"rep{n}" / "per_doc.jsonl"
        if not (lg.exists() and rr.exists()):
            break
        a, _, _ = load_records(lg)
        b, _, _ = load_records(rr)
        per_rep[f"rep{n}"] = cross_arm(a, b, "lg", "rr",
                                       require_byte_parity=matched)
    out["cross_arm"] = {
        "per_rep": per_rep,
        "byte_parity_gated": matched,
        "note": ("byte parity gated (matched Tika extractors)" if matched else
                 "byte parity measured but NOT gated (extractors differ)"),
        "PASS": bool(per_rep) and all(v.get("PASS") is True
                                      for v in per_rep.values()),
    }
    if not per_rep:
        out["cross_arm"]["error"] = "no rep present on both arms"

    provf = run / "provenance.json"
    out["provenance"] = (prov_check(json.loads(provf.read_text()))
                         if provf.exists()
                         else {"PASS": False, "error": "provenance.json missing"})

    # A successful execution is not proof of equal work. Preserve the historic
    # RUN verdict, but expose a separate gate for cross-framework claims.
    out["comparison_eligible"] = (
        set(out["arms"]) == {"langgraph", "rocketride"}
        and all(a["m0_PASS"] for a in out["arms"].values())
        and out["cross_arm"]["PASS"]
        and all(v.get("byte_parity") is True for v in per_rep.values())
        and all(not v.get("only_in_a") and not v.get("only_in_b")
                for v in per_rep.values())
        and out["provenance"]["PASS"]
        and not captures
    )

    (run / "RUN_REPORT.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))

    print("\n" + "=" * 74)
    print(f"MODE {mode.upper()} — warm-up excluded by the driver")
    if mode == "native_saturation":
        print("  NOT an equal-submission comparison: each arm runs its own")
        print("  native ingestion path. Equal 24-core cpuset is the fairness")
        print("  boundary, not equal interface or equal thread count.")
    for name, a in out["arms"].items():
        s = a["stability"]
        dup = (a["reps"][0] or {}).get("self_duplication") or {}
        am = a.get("mode", mode)
        lab = ("SERVICE" if (am.startswith("c") and am[1:].isdigit())
               else "BATCH-POSITION")
        print(f"\n{name} [{am}, latency={lab}]:  "
              f"M0 {'PASS' if a['m0_PASS'] else 'FAIL'}   "
              f"reps={a['rep_count']}/{a['rep_count_expected']}   "
              f"determinism={a['determinism_PASS']}   "
              f"self_dup={dup.get('duplicated_docs')}/{dup.get('checked')} "
              f"factors={dup.get('factors')}")
        for k in ("docs_per_s", "chunks_per_s", "p95_s", "effective_cores",
                  "cpu_utilization", "cpu_s_per_chunk"):
            v = s.get(k, {})
            if v.get("n"):
                print(f"  {k:<20} median={v.get('median')}  cv={v.get('cv')}  "
                      f"{v.get('verdict')}")
        u = (a["reps"][0] or {}).get("m2_latency", {}).get("unavailable")
        if u:
            print(f"  latency/concurrency  {u}")
    c = out["cross_arm"]
    print(f"\ncross-arm : {'identical' if c['PASS'] else 'DIVERGENT'}  "
          f"({len(c['per_rep'])} reps compared)  [REPORTED, not gated]")
    for rep, v in c["per_rep"].items():
        print(f"  {rep}: byte-identical {v['byte_identical']}/{v['compared']}  "
              f"ratio={v.get('chunk_ratio_rr_over_lg', {}).get('median')}  "
              f"hard_viol={len(v.get('hard_violations') or [])}")
    p = out["provenance"]
    print(f"\nprovenance: {'complete' if p['PASS'] else 'INCOMPLETE — ' + str(p.get('missing_fields') or p.get('error'))}")
    print("=" * 74)

    # Cross-arm byte parity is DELIBERATELY absent from this expression. It is
    # still computed, still names every differing doc, and is still the only
    # detector we have for the transposition defect -- but a known product
    # defect in one arm should not make the RUN unquotable. It is a finding
    # about RocketRide, reported above; the verdict below is about this run.
    ok = (bool(out["arms"])
          and all(a["m0_PASS"] for a in out["arms"].values())
          and p["PASS"] is True)      # incomplete provenance is not publishable

    # A bare FAIL hides which check fired -- and with a single rep the answer
    # is almost always "determinism is unproven", which says nothing about the
    # run's quality. Name the reasons so the verdict is readable.
    reasons = []
    if not out["arms"]:
        reasons.append("no arm produced records")
    for name, a in out["arms"].items():
        if a.get("m0_PASS"):
            continue
        if not a.get("rep_count_ok"):
            reasons.append(f"{name}: expected {a.get('rep_count_expected')} reps, "
                           f"got {a.get('rep_count')}")
        if a.get("determinism_PASS") is not True:
            reasons.append(f"{name}: determinism {'FAILED' if a.get('determinism_PASS') is False else 'UNPROVEN (needs >=2 reps, or a cross-mode run)'}")
        for r in a["reps"]:
            if r.get("error") or r.get("m0_PASS_partial"):
                continue
            for chk in ("census", "structure", "input_integrity", "self_duplication"):
                if (r.get(chk) or {}).get("PASS") is not True:
                    reasons.append(f"{name}/{r['rep_dir']}: {chk}")
    if p["PASS"] is not True:
        reasons.append("provenance incomplete")

    if captures:
        print("DIAGNOSTIC RUN — profiler enabled; not a baseline performance result")
    print("COMPARISON ELIGIBLE" if out["comparison_eligible"] else
          "COMPARISON NOT ESTABLISHED — check cross-arm parity, repetitions, provenance and profiling")
    print("RUN PASS" if ok else "RUN FAIL — numbers kept, never quoted")
    for why in reasons:
        print(f"   reason: {why}")
    if not ok and not reasons:
        print("   reason: unattributed — investigate before quoting")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
