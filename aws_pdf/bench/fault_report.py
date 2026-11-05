"""M4 blast radius + M5 fault isolation + byte-level DATA ISOLATION.

Data isolation is the check the other two cannot make. M4 asks whether the
neighbours still SUCCEEDED; isolation asks whether they produced the SAME
OUTPUT. A document can succeed while its content was quietly contaminated by a
concurrent failure, and only a byte comparison against a known-good baseline
catches that.

  python3 bench/fault_report.py <fault_run_dir> [baseline_run_dir]

baseline_run_dir is any earlier clean run over the same source documents; the
fault manifest carries each clean file's original name, which is how records
are matched across runs.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metrics.m0_correctness import census, self_duplication, structure
from metrics.m4_m5_faults import blast_radius, fault_isolation
from metrics.m7_resources import container_resources
from metrics.records import load_records, ok_records

ARMS = {"lg": "langgraph", "rr": "rocketride"}


def read_rc(p: Path):
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def data_isolation(rows, fault_docs, name_map, baseline):
    """Every UNRELATED doc must be byte-identical to its clean baseline.

    name_map: fault-corpus filename -> original filename (baseline key).
    Absence of a baseline is reported as unverified, never as a pass.
    """
    if not baseline:
        return {"PASS": None, "note": "no baseline supplied — UNVERIFIED"}
    fault_set = set(fault_docs)
    same = diff = unverified = 0
    changed = []
    for r in ok_records(rows):
        if r["doc"] in fault_set:
            continue
        orig = name_map.get(r["doc"])
        base = baseline.get(orig) if orig else None
        if base is None:
            unverified += 1
            continue
        if list(base) == list(r.get("chunk_sha256") or []):
            same += 1
        else:
            diff += 1
            changed.append(r["doc"])
    return {
        "compared": same + diff,
        "byte_identical_to_baseline": same,
        "altered": diff,
        "altered_docs": changed[:15],
        "unverified_no_baseline": unverified,
        # Vacuous is not a pass.
        "PASS": (same + diff) > 0 and diff == 0,
    }


def main():
    run = Path(sys.argv[1])
    base_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    fm = json.loads((run / "fault_manifest.json").read_text())
    fault_docs = [f["doc"] for f in fm["faults"]]
    name_map = {d["doc"]: d["source"] for d in fm["docs"] if d.get("source")}
    env = dict(l.split("=", 1) for l in (run / "environment.txt").read_text()
               .splitlines() if "=" in l)

    baseline, independent = {}, {}
    if base_dir:
        for arm in ARMS:
            p = base_dir / arm / "rep1" / "per_doc.jsonl"
            if p.exists():
                rows, _, _ = load_records(p)
                baseline[arm] = {r["doc"]: r.get("chunk_sha256")
                                 for r in ok_records(rows)}
                # Docs that fail in the CLEAN baseline fail regardless of any
                # fault; counting them as collateral manufactures damage.
                bad = {r["doc"] for r in rows if not r.get("ok")}
                independent[arm] = {d for d, o in name_map.items() if o in bad}

    out = {"run": run.name, "mode": env.get("mode"), "environment": env,
           "faults": fm["faults"], "arms": {}}

    for arm, name in ARMS.items():
        d = run / arm / "rep1"
        f = d / "per_doc.jsonl"
        if not f.exists():
            out["arms"][name] = {"error": f"missing {f}"}
            continue
        rows, meta, _ = load_records(f)
        rep = {"driver_rc": read_rc(d / "driver_rc.txt"),
               "mode": (meta or {}).get("mode")}

        rep["census"] = census(rows, offered=int(env.get("n_docs") or len(rows)),
                               expected_empty=set(fault_docs))
        rep["structure"] = structure(rows, arm=arm, expected_empty=set(fault_docs))
        rep["self_duplication"] = self_duplication(rows)

        # M4
        rep["m4_blast_radius"] = blast_radius(
            rows, fault_docs, independent_failures=independent.get(arm))

        # M5 — pre/post FAULT baselines, not run-boundary ones
        pre = d / "resources_pre_fault.jsonl"
        post = d / "resources_post_fault.jsonl"
        rb = container_resources(pre) if pre.exists() else None
        ra = container_resources(post) if post.exists() else None
        m5 = fault_isolation(rows, fault_docs, rb, ra)
        # restart_required is ORCHESTRATOR-recorded; the module leaves it None
        restarted = (d / "restart_required.txt")
        m5["restart_required"] = (restarted.read_text().strip() == "true"
                                  if restarted.exists() else None)
        m5["recovery_rc_before_restart"] = read_rc(d / "recovery_rc_before_restart.txt")
        m5["recovery_rc_after_restart"] = read_rc(d / "recovery_rc_after_restart.txt")
        for tag, sub in (("recovery", "recovery"),
                         ("recovery_after_restart", "recovery_after_restart")):
            p = d / sub / "per_doc.jsonl"
            if p.exists():
                rr_, _, _ = load_records(p)
                ok = ok_records(rr_)
                m5[f"{tag}_doc_ok"] = bool(ok)
                m5[f"{tag}_chunks"] = ok[0].get("n_chunks") if ok else None
        rep["m5_fault_isolation"] = m5

        rep["data_isolation"] = data_isolation(rows, fault_docs, name_map,
                                               baseline.get(arm))
        out["arms"][name] = rep

    (run / "FAULT_REPORT.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))

    print("\n" + "=" * 74)
    print(f"FAULT RUN — mode={out['mode']}  faults at "
          f"{[f['position'] for f in fm['faults']]}")
    for name, a in out["arms"].items():
        if a.get("error"):
            print(f"\n{name}: {a['error']}"); continue
        m4 = a["m4_blast_radius"]; m5 = a["m5_fault_isolation"]; di = a["data_isolation"]
        c = a["census"]
        print(f"\n{name} [{a.get('mode')}] driver_rc={a['driver_rc']}")
        print(f"  completed {c['completed']}/{c['offered']}  "
              f"failures={c['failed_by_reason']}")
        print(f"  M4 blast radius   : total collateral={m4['total_collateral']}  "
              f"zero-blast={m4['PASS_zero_blast']}")
        for fd, v in m4["per_fault"].items():
            if isinstance(v, dict) and "collateral_count" in v:
                print(f"     {fd[:34]:<34} outcome={str(v['fault_outcome'])[:22]:<22} "
                      f"collateral={v['collateral_count']:<4} "
                      f"next_ok={v['time_to_next_success_s']}s")
        print(f"  M5 server-surfaced: {m5['all_errors_surfaced']}   "
              f"service_continued={m5['service_continued']} ({m5['unrelated_ok']})")
        print(f"     surfaced by server : {m5['error_surfaced_by_server']}")
        print(f"     only client-inferred: {m5['failure_only_inferred_by_client']}")
        print(f"     restart_required={m5['restart_required']}  "
              f"recovery_ok={m5.get('recovery_doc_ok')}  "
              f"after_restart={m5.get('recovery_after_restart_doc_ok')}")
        print(f"     resources: {m5['resource_recovery']}")
        print(f"  DATA ISOLATION    : {di.get('byte_identical_to_baseline')}/"
              f"{di.get('compared')} byte-identical to clean baseline  "
              f"altered={di.get('altered')}  PASS={di['PASS']}")
        if di.get("altered_docs"):
            print(f"     ALTERED: {di['altered_docs']}")
    print("=" * 74)
    ok = all(a.get("m4_blast_radius", {}).get("PASS_zero_blast") and
             a.get("data_isolation", {}).get("PASS") is not False
             for a in out["arms"].values() if not a.get("error"))
    print("FAULT RUN PASS" if ok else "FAULT RUN — see findings above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
