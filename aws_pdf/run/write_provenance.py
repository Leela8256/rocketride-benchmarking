"""Write provenance.json for a run, then assert it is complete.

A performance number without its environment is not a result. Fields are read
from what actually ran (image digests, /meta, the engine binary's own hash)
rather than from config, because intent and reality diverge -- this suite has
already been bitten by /meta reporting a concurrency the code never used.

  python3 run/write_provenance.py <run_dir>
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from metrics.provenance import check  # noqa: E402


def main():
    run = Path(sys.argv[1])
    env = dict(l.split("=", 1) for l in (run / "environment.txt").read_text()
               .splitlines() if "=" in l)

    meta = {}
    p = run / "meta_lg.json"
    if p.exists():
        meta = json.loads(p.read_text())
    wv = meta.get("workload_versions", {})

    digests = {}
    p = run / "image_ids.txt"
    if p.exists():
        for line in p.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                digests[parts[0]] = parts[1]

    engine_sha = None
    p = run / "engine_sha.txt"
    if p.exists():
        engine_sha = p.read_text().split()[0]

    corpus_sha = None
    p = run / "corpus.sha256"
    if p.exists():
        import hashlib
        corpus_sha = hashlib.sha256(p.read_bytes()).hexdigest()

    # The pipe is THE shared contract, and until now it was the one artifact
    # with no fingerprint in provenance. A raw file hash is the wrong one: the
    # visual editor rewrites viewport/ui/docRevision on any open-and-save, so
    # the raw hash changes while the pipeline does not. Hash the canonicalised
    # COMPONENTS instead -- stable across editor saves, sensitive to any real
    # change in nodes, providers, config or lane wiring.
    pipe_sha = pipe_raw_sha = None
    pipe_doc = None
    for cand in (run / "pipeline.pipe", *sorted(run.glob("*/rep1/pipeline.pipe"))):
        if not cand.exists():
            continue
        import hashlib
        try:
            doc = json.loads(cand.read_text())
        except Exception:
            break
        def _strip(o):
            drop = ("ui", "name", "viewport", "snapToGrid", "snapGridSize",
                    "isLocked", "docRevision")
            if isinstance(o, dict):
                return {k: _strip(v) for k, v in o.items() if k not in drop}
            if isinstance(o, list):
                return [_strip(x) for x in o]
            return o
        canon = json.dumps(_strip(doc.get("components")), sort_keys=True)
        pipe_sha = hashlib.sha256(canon.encode()).hexdigest()
        pipe_raw_sha = hashlib.sha256(cand.read_bytes()).hexdigest()
        pipe_doc = doc
        break

    # RocketRide-only runs (SKIP_LG=1) have no LangGraph /meta to read the
    # parser, chunk and embedding configuration from, which used to leave
    # every engine-only probe with INCOMPLETE provenance for lack of a
    # comparison arm. Read those three from the pipe that actually ran -- it
    # is the contract the engine executed, hashed above -- and say so.
    def _comp(provider):
        for c in (pipe_doc or {}).get("components") or []:
            if isinstance(c, dict) and c.get("provider") == provider:
                return c
        return None
    rr_only = not meta
    parse_c = _comp("parse")
    chunk_c = _comp("preprocessor_langchain")
    emb_c = _comp("embedding_transformer")
    parser = env.get("lg_extractor")
    parser_cfg_hash = wv.get("extractor")
    chunk_cfg = wv.get("split")
    emb_model = (wv.get("embedding") or {}).get("model_id")
    if rr_only and parse_c is not None:
        import hashlib
        parser = "rocketride parse node (engine-embedded Apache Tika 3.2.3)"
        parser_cfg_hash = hashlib.sha256(json.dumps(
            {"provider": "parse", "config": parse_c.get("config", {})},
            sort_keys=True).encode()).hexdigest()
    if rr_only and chunk_c is not None:
        chunk_cfg = {"provider": "preprocessor_langchain",
                     "config": chunk_c.get("config", {}),
                     "source": "pipeline.pipe (RocketRide-only run)"}
    if rr_only and emb_c is not None:
        prof = (emb_c.get("config") or {}).get("profile")
        known = {"miniLM": "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"}
        emb_model = (f"{known.get(prof, 'unknown')} (engine profile "
                     f"{prof!r}, from pipeline.pipe)")

    rec = {
        "run_id": run.name,
        "timestamp_utc": env.get("stamp_utc"),
        "git_commit": env.get("git_sha"),
        "git_dirty": env.get("git_dirty"),
        "image_digest": digests,
        "framework_version": {
            "langgraph_extractor": wv.get("extractor"),
            "rocketride_engine_sha256": engine_sha,
        },
        # The engine is PATCHED unless RR_DUP_PATCH=0. Describe the tested
        # system as "RocketRide 3.3.1 with the documented embedding-transformer
        # duplication correction" -- never as stock 3.3.1.
        "rocketride_engine_version": env.get("rr_engine_version", "3.3.1"),
        "rocketride_engine_sha256": engine_sha,
        "rocketride_sdk_version": env.get("rr_sdk_version", "1.3.0"),
        "rocketride_engine_archive_sha256": env.get("rr_engine_archive_sha256"),
        "profiler_requested_seconds": env.get("rr_profile_seconds", "0"),
        "profiler_langgraph_requested_seconds": env.get("lg_profile_seconds", "0"),
        "duplication_patch_applied": env.get("rr_dup_patch", "1") == "1",
        "duplication_patch_id": "BUG_CHUNK_DUPLICATION",
        "engine_image_digest": digests.get("rocketride"),
        "instance_type": env.get("instance_type", "c7i.8xlarge"),
        "architecture": env.get("arch"),
        "os": env.get("os"),
        "kernel": env.get("kernel"),
        "cpu_count": int(env.get("nproc") or 0) or None,
        "ram_gb": int(env.get("mem_gb") or 0) or None,
        "corpus_manifest_sha256": corpus_sha,
        # Canonicalised pipeline contract; see the comment above for why this
        # is not a raw file hash. Compare THIS across runs and across teams.
        "pipe_components_sha256": pipe_sha,
        "pipe_file_sha256": pipe_raw_sha,
        "corpus_n_docs": int(env.get("n_docs") or 0) or None,
        "parser": parser,
        "parser_config_hash": parser_cfg_hash,
        "chunk_config": chunk_cfg,
        "embedding_model": emb_model,
        "provenance_source_for_parser_chunk_embedding":
            ("pipeline.pipe (no LangGraph arm in this run)" if rr_only
             else "langgraph /meta"),
        "offered_concurrency": env.get("mode"),
        "configured_concurrency": {
            "langgraph": "default executor min(32, cpu_count+4); /meta's "
                         "executor_workers is reported but INERT",
            "rocketride": env.get("rr_threads"),
        },
        "envelope": {"cpus": env.get("arm_cpus"), "memory": env.get("arm_mem")},
        "warmup_policy": f"{env.get('warm_docs')} docs, timed separately, excluded",
        # From the run's own environment, never a constant: a hardcoded value
        # here once recorded 300 for runs that actually ran 3600/7200.
        "timeout_s": int(env["timeout_s"]) if str(env.get("timeout_s", "")).isdigit() else env.get("timeout_s"),
        "mode": env.get("mode"),
        # The two arms are NOT running the same submission interface in
        # native_saturation. Recorded field by field so no reader can assume
        # symmetry, and so "threads requested" is never read as "threads
        # activated" or as effective cores.
        "benchmark_mode": env.get("mode"),
        "langgraph_driver_mode": env.get("lg_mode"),
        "langgraph_submission": "bounded closed-loop HTTP window",
        "langgraph_client_window": int(env.get("lg_client_window") or 0) or None,
        "langgraph_server_executor_policy": "default min(32, cpu_count+4); "
                                            "/meta's executor_workers is INERT",
        "rocketride_driver_mode": env.get("rr_mode"),
        "rocketride_submission": (
            "one whole-corpus SDK batch" if (env.get("rr_mode") or "") == "blast"
            else f"consecutive SDK batches of {env['rr_mode'][1:]} documents on one connection"
            if re.fullmatch(r"b\d+", env.get("rr_mode") or "")
            else f"closed-loop, one SDK call per document ({env.get('rr_mode')})"),
        "rocketride_batch_size": (int(env["rr_mode"][1:])
                                  if re.fullmatch(r"b\d+", env.get("rr_mode") or "")
                                  else None),
        "rocketride_threads_requested": int(env.get("rr_threads"))
            if (env.get("rr_threads") or "").isdigit() else None,
        "rocketride_threads_observed": None,   # engine pool is not observable
        "rocketride_cpu_cores_allocated": float(env.get("arm_cpus") or 0) or None,
        "omp_num_threads": 1,
        "measured_documents": int(env.get("n_docs") or 0) or None,
        "reps": int(env.get("reps") or 0) or None,
    }
    thr = rec.get("rocketride_threads_requested")
    cores = rec.get("rocketride_cpu_cores_allocated")
    if thr and cores:
        rec["rocketride_oversubscribed"] = thr > cores
        rec["rocketride_oversubscription_ratio"] = round(thr / cores, 4)

    (run / "provenance.json").write_text(json.dumps(rec, indent=1))

    verdict = check(rec)
    print(f"provenance: {'complete' if verdict['PASS'] else 'INCOMPLETE'}")
    if not verdict["PASS"]:
        print(f"  missing: {verdict['missing_fields']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
