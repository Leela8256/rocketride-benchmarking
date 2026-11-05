"""The video benchmark report — gates first, numbers second, fail-closed.

Every metric is re-derivable from the raw records forever: download a run
from S3 and get the same numbers on any machine.

  python3 bench/report.py <run_dir>                        one run
  python3 bench/report.py <rep1> <rep2> [rep3...]          reps of ONE arm
                                                           (adds determinism)
  python3 bench/report.py --arms <rrA[,rrB...]> <lgA[,lgB...]>
                                                           cross-arm gates too

A run_dir needs per_doc.jsonl; engine_cgroup.csv, manifest.json and
progress.jsonl are used when present. Exit non-zero if any HARD gate
fails (SKIP/WARN are surfaced, not fatal — but a SKIP on a gate the claim
depends on means the claim cannot be made).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from metrics import v0_gates as v0
from metrics import v_metrics as vm


def load_run(d):
    d = Path(d)
    recs, meta = [], {}
    for line in open(d / "per_doc.jsonl"):
        r = json.loads(line)
        if r.get("kind") == "shot_meta":
            meta = r
        else:
            recs.append(r)
    manifest = {}
    if (d / "manifest.json").exists():
        manifest = json.loads((d / "manifest.json").read_text())
    # ENGINE cgroup only. The matched runner also drops driver_cgroup.csv (the
    # client container, measured separately) beside it — merging two cumulative
    # counters interleaves them and the windowed delta goes negative (bitten
    # on the first matched smoke).
    sampler = []
    files = [d / "engine_cgroup.csv"] if (d / "engine_cgroup.csv").exists() else \
        [f for f in sorted(d.glob("*cgroup*.csv")) if "driver" not in f.name]
    for f in files:
        for line in open(f):
            p = line.strip().split(",")
            if p and p[0].isdigit():
                sampler.append(tuple(int(x) for x in p if x.lstrip("-").isdigit()))
    driver = []
    if (d / "driver_cgroup.csv").exists():
        for line in open(d / "driver_cgroup.csv"):
            p = line.strip().split(",")
            if p and p[0].isdigit():
                driver.append(tuple(int(x) for x in p if x.lstrip("-").isdigit()))
    progress = []
    if (d / "progress.jsonl").exists():
        progress = [json.loads(l) for l in open(d / "progress.jsonl")]
    return {"dir": str(d), "records": recs, "meta": meta,
            "manifest": manifest, "sampler": sampler, "progress": progress,
            "driver_sampler": driver}


def gate_line(g):
    mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "warn", "SKIP": "SKIP"}[g["status"]]
    return f"  [{mark}] {g['gate']:18s} {g['detail']}"


def posture_gates(meta):
    """Matched-posture fail-closed gates: the census/readbacks that make
    '8 real processes' a verified fact. Default postures get no extra gate."""
    p = meta.get("posture") or ""
    out = []
    if p.startswith("rr_matched"):
        c = meta.get("task_census") or {}
        k = meta.get("tasks")
        ok = (len(c.get("new_task_pids") or []) == k
              and len(set(c.get("project_ids") or [])) == k
              and c.get("tokens_distinct") is True
              and len(c.get("environ_readback") or {}) == k)
        out.append(v0._g("task_census", "PASS" if ok else "FAIL",
                         f"{len(c.get('new_task_pids') or [])}/{k} task processes, "
                         f"{len(set(c.get('project_ids') or []))} project ids, environ "
                         f"readback for {len(c.get('environ_readback') or {})}"))
    elif p.startswith("lg_matched") or p.startswith("lg_inproc"):
        # The gate asserts the posture that was ASKED FOR, read back from the
        # live service — not a fixed value. (It hardcoded 1 for the original
        # 8x4 cell, which failed every config-sweep cell that sets it higher.)
        rb = meta.get("worker_readbacks") or []
        w = meta.get("workers")
        exp = (meta.get("provenance") or {}).get("detect_concurrency_per_process")
        pids = {m.get("pid") for m in rb}
        ok = (len(rb) == w and len(pids) == w and None not in pids
              and all((m.get("torch") or {}).get("num_interop_threads") == 1 for m in rb)
              and (exp is None
                   or all(m.get("detect_concurrency_per_process") == exp for m in rb)))
        out.append(v0._g("worker_census", "PASS" if ok else "FAIL",
                         f"{len(pids)}/{w} distinct worker pids, detect-concurrency "
                         f"{exp}/process, interop 1 — all read back from /meta"))
    return out


def run_gates(run):
    recs, man = run["records"], run["manifest"].get("docs") or \
        [r["doc"] for r in run["records"]]
    return [v0.census(recs, man), v0.structure(recs),
            v0.frame_law(recs), v0.self_duplication(recs),
            v0.corpus_pin(recs, run["manifest"])] + posture_gates(run["meta"])


def report_one(run):
    print(f"\n== {run['dir']}  arm={run['meta'].get('arm', '?')} "
          f"posture={run['meta'].get('posture', 'default')} "
          f"mode={run['meta'].get('mode', '?')}")
    gates = run_gates(run)
    for g in gates:
        print(gate_line(g))
    hard_fail = any(g["status"] == "FAIL" for g in gates)
    if hard_fail:
        print("  GATES FAILED — numbers below are diagnostic only, not quotable")
    v1 = vm.v1_throughput(run["records"], run["meta"])
    v2 = vm.v2_latency(run["records"], run["meta"], run["progress"])
    # Driver measurement markers (2026-08-23) window the sampler so CPU
    # excludes warm-up/startup; older runs without markers fall back to the
    # full span and the basis string says so.
    s_ns = run["meta"].get("measurement_start_epoch_ns")
    e_ns = run["meta"].get("measurement_end_epoch_ns")
    window = (s_ns / 1e9, e_ns / 1e9) if s_ns and e_ns else None
    cpu = vm.cpu_from_sampler(run["sampler"], window)
    v3 = vm.v3_efficiency(run["records"], run["meta"], cpu)
    v4 = vm.v4_resources(run["records"], run["meta"], cpu)
    # Driver-side CPU (client container), measured separately, never added to
    # the arm; idle burden = engine cores in the post-warm-up quiet window.
    if run.get("driver_sampler"):
        dcpu = vm.cpu_from_sampler(run["driver_sampler"], window)
        if dcpu:
            v4["driver_cpu_s_in_window"] = dcpu["cpu_s"]
            v4["driver_effective_cores"] = dcpu["effective_cores"]
    idle = run["meta"].get("idle_window_epoch_ns")
    if idle and run["sampler"]:
        icpu = vm.cpu_from_sampler(run["sampler"], (idle[0] / 1e9, idle[1] / 1e9))
        if icpu:
            v4["idle_burden_cores"] = icpu["effective_cores"]
    v5 = vm.v5_cost(v1)
    for name, block in (("V1 throughput", v1), ("V2 latency", v2),
                        ("V3 efficiency", v3), ("V4 resources", v4),
                        ("V5 cost", v5)):
        print(f"  {name}: {json.dumps(block)}")
    cov = vm.coverage(
        {"V1": v1, "V2": v2, "V3": v3, "V5": v5},
        exemptions=("frames" if any(g["gate"] == "frame_law" and
                                    g["status"] == "SKIP" for g in gates) else "",
                    "threads_activated",          # sampler-version dependent
                    "time_to_first_result",       # mode dependent
                    "cpu_s_per_frame", "cpu_s_per_detection",
                    "peak_mem", "cold_to_ready",
                    "mean_measured_video"))       # error-path only
    print(gate_line(cov))
    gates.append(cov)
    print(f"  envelope: {run['meta'].get('envelope', 'NOT RECORDED')}")
    run["v1"] = v1
    return gates


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    all_gates = []

    if args[0] == "--arms":
        rr_dirs, lg_dirs = args[1].split(","), args[2].split(",")
        rr_runs = [load_run(d) for d in rr_dirs]
        lg_runs = [load_run(d) for d in lg_dirs]
        for run in rr_runs + lg_runs:
            all_gates += report_one(run)
        for name, runs in (("rocketride", rr_runs), ("langgraph", lg_runs)):
            g = v0.determinism([{r["doc"]: r for r in x["records"]} for x in runs])
            print(f"\n== determinism [{name}]")
            print(gate_line(g))
            all_gates.append(g)
        print("\n== cross-arm (rep1 vs rep1)")
        a = {r["doc"]: r for r in rr_runs[0]["records"]}
        b = {r["doc"]: r for r in lg_runs[0]["records"]}
        for g in ([v0.input_identity(a, b)] + v0.cross_arm(a, b, "rr", "lg")
                  + [v0.chunk_parity_tight(a, b)]):
            print(gate_line(g))
            all_gates.append(g)
        print(f"  workload_ratio_rr_over_lg: {v0.workload_ratio(a, b)}")
    else:
        runs = [load_run(d) for d in args]
        for run in runs:
            all_gates += report_one(run)
        if len(runs) > 1:
            g = v0.determinism([{r["doc"]: r for r in x["records"]} for x in runs])
            print("\n== determinism (across given runs)")
            print(gate_line(g))
            all_gates.append(g)
            by_mode = {x["meta"].get("mode"): x.get("v1", {}) for x in runs}
            conc = next((int(m[1:]) for m in by_mode if m and m.startswith("c")), None)
            cm = vm.cross_mode(by_mode, conc)
            if cm:
                print(f"  cross-mode: {json.dumps(cm)}")
        else:
            print("\n  NOTE: single run — determinism unproven; not a "
                  "benchmark result (sizing evidence only)")

    # Split verdict (exit-semantics review 2026-08-23): hard-gate validity
    # drives the exit code; determinism at 1 rep is NOT_RUN, downgrading the
    # EVIDENCE GRADE instead of masquerading as a processing failure. The
    # wrapper combines this exit with the arms' own exit codes for
    # execution_status.
    fails = [g for g in all_gates if g["status"] == "FAIL"]
    skips = [g for g in all_gates if g["status"] == "SKIP"]
    det = [g for g in all_gates if g["gate"] == "determinism"]
    det_status = ("FAIL" if any(g["status"] == "FAIL" for g in det) else
                  "NOT_RUN" if (not det or
                                any("NOT_RUN" in g["detail"] for g in det)) else
                  "PASS")
    validity = "FAIL" if fails else "PASS"
    grade = ("SIZING — single repetition, no determinism evidence; not a "
             "publishable comparison" if det_status == "NOT_RUN" else
             "REPEATED — determinism exercised; publishable claims still "
             "need a shared CPU envelope and >=3 reps")
    print(f"\n== verdict")
    print(f"  validity_status: {validity} "
          f"({len(fails)} hard failures, {len(skips)} skipped gates)")
    print(f"  determinism: {det_status}")
    print(f"  evidence_grade: {grade}")
    if fails:
        for g in fails:
            print(f"  FAILED: {g['gate']}: {g['detail'][:120]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
