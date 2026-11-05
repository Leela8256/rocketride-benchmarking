"""RocketRide MATCHED-POSTURE driver — posture ``rr_matched_8x4``.

Eight-process/model architecture-matched comparison using native ingestion:
RocketRide sharded blast versus LangGraph c32 HTTP saturation. This is a
SEPARATE posture from bench_video.py (``rr_default_1token``); the default
driver and its historical results are untouched.

What is different from the default driver:
  - K (default 8) genuinely independent tasks: K runtime pipe copies, each
    with a distinct deterministic project_id, one use() per copy on its OWN
    client connection, NO use_existing (the server would return the existing
    task — task identity is (owner, project_id, source)), threads= only if
    RR_THREADS_PER_CONN is set (that
    is per-task item concurrency, engine default 64, not torch threads)
  - ttl=0 = no expiry (task_server.py: "Tasks with ttl=0 have no timeout")
  - FAIL-CLOSED task census: after EACH use() the runner snapshots the engine
    container's processes (handshake over the shared results volume — the
    driver cannot see engine PIDs); the delta must be exactly one new task
    process, and its /proc/<pid>/environ must carry the six BLAS/OMP vars
    at the expected value. Any miss aborts before warm-up.
  - every task warmed with BOTH disjoint warm fixtures (excluded from
    measurement); an idle-burden window is sampled after warm-up
  - measured docs sharded round-robin in committed order (index % K); K
    concurrent send_files() released at a barrier; span = barrier -> last
    result; per-doc records carry token index, project_id, task PID, shard
    index

  python3 bench_video_matched.py <corpus_dir> <out_dir> <n_docs> <warm_dir>

Env: RR_TASKS (8), RR_PIPE_TTL_S (0), BENCH_TIMEOUT_S, RR_BLAS_THREADS (4,
the value expected in every task's environ), BENCH_IDLE_S (30), BENCH_PIPE.
"""

import asyncio
import collections
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_video import (documents_from, fill_from_docs, base_record,   # noqa: E402
                         VIDEO_DURS)

URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride-matched:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_video_detect.pipe"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "86400"))
K = int(os.environ.get("RR_TASKS", "8"))
TTL_S = int(os.environ.get("RR_PIPE_TTL_S") or 0)
BLAS_THREADS = os.environ.get("RR_BLAS_THREADS", "4")
# RR_THREADS_PER_CONN: engine per-connection admission cap, passed as
# use(threads=N). Per the engine team (rocketride-server#2053, 2026-08-26):
# in-flight width = connections x threads, a slot held for each document's
# whole open->write->close. Unset = omitted (engine default 64, how the
# 2026-08-26 8x4 run ran). Validated 1-64 by the engine; 256 was rejected.
_t = os.environ.get("RR_THREADS_PER_CONN", "").strip()
ITEM_THREADS = int(_t) if _t else None
IDLE_S = int(os.environ.get("BENCH_IDLE_S", "30"))
POSTURE = f"rr_matched_{K}x{BLAS_THREADS}"
ARM = "rocketride-docker-3.3.1-video"
ENGINE_DEFAULT_ITEM_THREADS = 64      # CONST_DEFAULT_MAX_THREADS in the engine
BLAS_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
             "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS")
# What a RocketRide task process looks like (probed 2026-08-25 on engine
# 3.3.1): child of the eaas.py server, cmdline
#   /opt/rocketride/engine/engine /opt/rocketride/engine/ai/node.py \
#       /tmp/task-<hash>.<source>-<id>.json --autoterm --monitor=app ...
# One use() == one such process. Nothing in the container has "python" in
# its cmdline (the engine binary is ./engine), hence this marker.
TASK_MARKER = os.environ.get("RR_TASK_CMD_MARKER", "node.py /tmp/task-")


def task_pids(snap: dict) -> set:
    return {int(p) for p, c in (snap.get("cmd") or {}).items()
            if TASK_MARKER in c and p.isdigit()}


def redact(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def census_snapshot(census_dir: Path, seq: int, want_env: bool) -> dict:
    """Ask the runner (host side) for a process snapshot of the engine
    container and wait for it. Protocol: <seq>.request -> <seq>.json."""
    req = census_dir / f"{seq}.request"
    resp = census_dir / f"{seq}.json"
    req.write_text(json.dumps({"seq": seq, "want_env": want_env}))
    for _ in range(600):
        if resp.exists():
            try:
                return json.loads(resp.read_text())
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.5)
    raise SystemExit(f"CENSUS TIMEOUT: runner never answered snapshot {seq} "
                     f"(is the census watcher running?)")


def ordered_corpus(corpus_dir: Path, n: int):
    order_file = corpus_dir / "measured_order.txt"
    if order_file.exists():
        names = [l.strip() for l in open(order_file) if l.strip() and not l.startswith("#")]
        vids = [corpus_dir / nm for nm in names]
        missing = [v.name for v in vids if not v.exists()]
        if missing:
            raise SystemExit(f"measured_order.txt lists missing files: {missing[:3]}")
    else:
        vids = sorted(list(corpus_dir.glob("*.avi")) + list(corpus_dir.glob("*.mp4")))
    return vids[:n]


async def main():
    from rocketride import RocketRideClient

    corpus_dir, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    warm_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    corpus = ordered_corpus(corpus_dir, n)
    if len(corpus) != n:
        raise SystemExit(f"requested {n} measured docs, found {len(corpus)}")
    warm_set = sorted(list(warm_dir.glob("*.avi")) + list(warm_dir.glob("*.mp4"))) if warm_dir else []
    if not warm_set:
        raise SystemExit("matched posture requires disjoint warm fixtures in <warm_dir>")
    if set(v.name for v in warm_set) & set(v.name for v in corpus):
        raise SystemExit("warm fixtures overlap the measured corpus")
    out.mkdir(parents=True, exist_ok=True)
    census_dir = out / "census"
    census_dir.mkdir(exist_ok=True)
    pipes_dir = out / "pipes"
    pipes_dir.mkdir(exist_ok=True)

    durations, corpus_shas, video_durs = {}, {}, {}
    mf = corpus_dir / "corpus_manifest.json"
    if mf.exists():
        _m = json.loads(mf.read_text())
        durations = _m.get("duration_s", {})
        corpus_shas = _m.get("sha256", {})
        video_durs = _m.get("video_duration_s", {})
        VIDEO_DURS.update(video_durs)
    measured_video_s = sum(video_durs.get(v.name) or durations.get(v.name) or 0
                           for v in corpus)
    measured_source_s = sum(durations.get(v.name) or 0 for v in corpus)

    # Sharding: round-robin in committed order — no longest-first.
    shards = [[] for _ in range(K)]
    for i, v in enumerate(corpus):
        shards[i % K].append((i, v))

    (out / "manifest.json").write_text(json.dumps(
        {"docs": [v.name for v in corpus], "n": len(corpus),
         "posture": POSTURE,
         "measured_video_s": measured_video_s,
         "measured_source_s": measured_source_s,
         "sha256": {v.name: corpus_shas[v.name] for v in corpus if v.name in corpus_shas},
         "video_duration_s": {v.name: video_durs[v.name] for v in corpus if v.name in video_durs},
         "shard_map": {v.name: i % K for i, v in enumerate(corpus)},
         "warm_fixtures": [w.name for w in warm_set]}))

    base_pipe = json.loads(PIPE_SRC.read_text())
    project_ids = [f"videobench-matched{K}x{BLAS_THREADS}-{k:02d}" for k in range(K)]
    pipe_paths = []
    for k, pid in enumerate(project_ids):
        p = dict(base_pipe)
        p["project_id"] = pid
        pp = pipes_dir / f"pipe_{k:02d}.pipe"
        pp.write_text(json.dumps(p))
        pipe_paths.append(pp)
    assert len(set(project_ids)) == K

    print(f"[rrm] posture={POSTURE} tasks={K} corpus={len(corpus)} docs "
          f"({measured_video_s/3600:.2f} h probed), warm={[w.name for w in warm_set]}, "
          f"ttl={TTL_S} (0 = no expiry), "
          f"threads_arg={ITEM_THREADS if ITEM_THREADS is not None else 'OMITTED'} "
          f"(effective item threads {ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS}, "
          f"admission width {K * (ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS)} box-wide), "
          f"blas_expected={BLAS_THREADS}", flush=True)

    # ---- task creation with per-call census (fail-closed) ----
    pre = await census_snapshot(census_dir, 0, want_env=False)
    pids_before = task_pids(pre)
    if pids_before:
        raise SystemExit(f"TASK CENSUS FAIL: {len(pids_before)} task process(es) already "
                         f"alive before creation {sorted(pids_before)} — box not quiet "
                         f"(stale ttl=0 tasks?)")
    all_before = set(pre["pids"])
    clients, tokens, task_pid_list, env_readback, task_cmdlines = [], [], [], {}, {}
    event_times = [dict() for _ in range(K)]
    event_actions = collections.Counter()
    run_t0 = time.perf_counter_ns()
    progress_fh = open(out / "progress.jsonl", "a", buffering=1)

    def make_on_event(k):
        async def on_event(ev):
            try:
                if ev.get("event") != "apaevt_status_upload":
                    return
                b = ev.get("body") or {}
                act = b.get("action")
                event_actions[act] += 1
                if act in ("complete", "error"):
                    fp = b.get("filepath")
                    if fp:
                        name = Path(fp).name
                        event_times[k].setdefault(name, time.perf_counter_ns())
                        progress_fh.write(json.dumps(
                            {"doc": name, "action": act, "token_index": k,
                             "t_rel_s": round((time.perf_counter_ns() - run_t0) / 1e9, 1),
                             "n_done": sum(len(e) for e in event_times)}) + "\n")
            except Exception:
                pass
        return on_event

    seen = set(pids_before)
    for k in range(K):
        c = RocketRideClient(uri=URI, auth=APIKEY, on_event=make_on_event(k))
        await c.connect()
        use_kw = dict(filepath=str(pipe_paths[k]), ttl=TTL_S)        # no use_existing
        if ITEM_THREADS is not None:
            use_kw["threads"] = ITEM_THREADS                            # per-connection admission cap
        used = await c.use(**use_kw)
        tok = used["token"]
        clients.append(c)
        tokens.append(tok)
        try:
            await c.set_events(tok, ["apaevt_status_upload"])
        except Exception as exc:
            print(f"[rrm] set_events unavailable on task {k} ({type(exc).__name__})", flush=True)
        snap = await census_snapshot(census_dir, k + 1, want_env=True)
        new = sorted(task_pids(snap) - seen)
        extra = sorted(set(snap["pids"]) - all_before - task_pids(snap))
        if len(new) != 1:
            for cc, tt in zip(clients, tokens):
                try:
                    await cc.terminate(tt)
                except Exception:
                    pass
            raise SystemExit(f"TASK CENSUS FAIL at task {k}: expected exactly 1 new "
                             f"task process ({TASK_MARKER!r}), saw {len(new)} {new[:5]}; "
                             f"other new processes {extra[:5]} — use() deduped or "
                             f"spawned unexpectedly; aborting before warm-up")
        pid = new[0]
        seen.add(pid)
        task_pid_list.append(pid)
        task_cmdlines[str(pid)] = (snap.get("cmd") or {}).get(str(pid), "")
        env = (snap.get("env") or {}).get(str(pid)) or {}
        env_readback[str(pid)] = env
        bad = [v for v in BLAS_VARS if env.get(v) != BLAS_THREADS]
        if bad:
            raise SystemExit(f"BLAS/OMP READBACK FAIL: task pid {pid} environ has "
                             f"{ {v: env.get(v) for v in bad} }, expected {BLAS_THREADS}")
        print(f"[rrm] task {k}: project_id={project_ids[k]} token={redact(tok)} "
              f"pid={pid} environ OK ({BLAS_THREADS} x6)", flush=True)
    if len(set(tokens)) != K:
        raise SystemExit("TOKEN IDENTITY FAIL: tokens not distinct")

    census = {"declared_tasks": K, "task_pids_before": sorted(pids_before),
              "task_pids_after": sorted(seen), "new_task_pids": task_pid_list,
              "task_cmdlines": task_cmdlines, "task_marker": TASK_MARKER,
              "project_ids": project_ids, "tokens_distinct": True,
              "token_digests": [redact(t) for t in tokens],
              "environ_readback": env_readback,
              "torch_interop_readback": "UNVERIFIABLE — engine task process; "
                                        "not settable via env, disclosed"}
    (out / "task_census.json").write_text(json.dumps(census, indent=1))

    # ---- warm-up: every task gets BOTH fixtures; excluded from measurement ----
    warm_records = []
    async def warm(k):
        tw = time.perf_counter_ns()
        res = await asyncio.wait_for(
            clients[k].send_files([(str(p), {"doc_id": f"warm-{k}-{p.stem}"}) for p in warm_set],
                                  tokens[k]), timeout=TIMEOUT_S)
        items = res if isinstance(res, list) else [res]
        ok = sum(1 for it in items if isinstance(it, dict) and documents_from(it))
        warm_records.append({"token_index": k, "task_pid": task_pid_list[k],
                             "fixtures": [p.name for p in warm_set], "docs_ok": ok,
                             "warm_s": round((time.perf_counter_ns() - tw) / 1e9, 1)})
        return ok
    warm_ok = await asyncio.gather(*[warm(k) for k in range(K)])
    for k, ok in enumerate(warm_ok):
        if ok < len(warm_set):
            raise SystemExit(f"WARM FAIL: task {k} (pid {task_pid_list[k]}) returned "
                             f"{ok}/{len(warm_set)} warm docs")
    for e in event_times:
        e.clear()
    event_actions.clear()
    print(f"[rrm] warm-up: all {K} tasks warmed on {len(warm_set)} fixtures "
          f"(excluded from measurement)", flush=True)

    # ---- idle-burden window (sampled by the runner via the same cgroup) ----
    mono_offset_ns = time.time_ns() - time.perf_counter_ns()
    idle_start = time.time_ns()
    await asyncio.sleep(IDLE_S)
    idle_end = time.time_ns()

    # ---- barrier + K concurrent sharded blasts ----
    # RR_INFLIGHT_PER_TASK bounds how many of a shard's videos are in flight on
    # a task at once (default 4 = LangGraph matched's 4 requests per worker,
    # 8x4 = 32 box-wide). 0 = one native batch per shard: the engine then
    # admits up to threads=64 items per task, i.e. ~21 concurrent ffmpeg
    # decoders PER TASK on AMI-168 — the first full run showed 114 decoders,
    # memory pinned at the 58g limit and ~66x instead of ~180x. Admission is
    # an ingestion-policy dimension, disclosed in shot_meta either way.
    INFLIGHT = int(os.environ.get("RR_INFLIGHT_PER_TASK", "4") or 0)

    async def blast(k):
        items = shards[k]
        if not items:
            return []
        if INFLIGHT <= 0:
            payload = [(str(v), {"doc_id": v.stem}) for _, v in items]
            return await asyncio.wait_for(clients[k].send_files(payload, tokens[k]),
                                          timeout=TIMEOUT_S)
        sem = asyncio.Semaphore(INFLIGHT)

        async def one(v):
            async with sem:
                try:
                    r = await asyncio.wait_for(
                        clients[k].send_files([(str(v), {"doc_id": v.stem})], tokens[k]),
                        timeout=TIMEOUT_S)
                    return r if isinstance(r, list) else [r]
                except Exception as exc:          # attribute the failure to its file
                    return [{"filepath": str(v), "error": f"{type(exc).__name__}: {exc}"[:250]}]
        parts = await asyncio.gather(*[one(v) for _, v in items])
        return [it for part in parts for it in part]
    coros = [blast(k) for k in range(K)]
    t0 = time.perf_counter_ns()
    results = await asyncio.gather(*coros, return_exceptions=True)
    span_ns = time.perf_counter_ns() - t0
    span = span_ns / 1e9

    records = []
    for k in range(K):
        res = results[k]
        by_name = {}
        if isinstance(res, Exception):
            print(f"[rrm] shard {k} send_files raised {type(res).__name__}: {res}"[:200], flush=True)
        else:
            for it in (res if isinstance(res, list) else [res]):
                if isinstance(it, dict) and isinstance(it.get("filepath"), str):
                    by_name.setdefault(Path(it["filepath"]).name, it)
        for j, (i, v) in enumerate(shards[k]):
            rec = base_record(v, durations)
            rec.update({"manifest_index": i, "token_index": k, "project_id": project_ids[k],
                        "task_pid": task_pid_list[k], "shard_local_index": j,
                        "submit_ns": t0, "timing_source": "batch_upload_time (derived, not measured)"})
            it = by_name.get(v.name)
            if isinstance(res, Exception) or it is None:
                rec["completion_ns"] = t0 + span_ns
                rec["identity_ok"] = False
                rec["reason"] = "engine_error" if isinstance(res, Exception) else "no_response_for_file"
                if isinstance(res, Exception):
                    rec["error"] = f"{type(res).__name__}: {res}"[:250]
                records.append(rec)
                continue
            ev = event_times[k].get(v.name)
            if ev is not None:
                rec["completion_ns"] = ev
                rec["timing_source"] = "client-observed upload event"
            elif isinstance(it.get("upload_time"), (int, float)):
                rec["completion_ns"] = t0 + int(float(it["upload_time"]) * 1e9)
            else:
                rec["completion_ns"] = t0 + span_ns
            rec["upload_time_s"] = it.get("upload_time")
            rec["identity_ok"] = True
            err = it.get("error") or it.get("status_error")
            docs = documents_from(it)
            okv, why = fill_from_docs(rec, docs)
            if err and not docs:           # explicit engine error beats "no documents"
                rec["ok"] = False
                rec["reason"] = "engine_error"
                rec["error"] = str(err)[:250]
            else:
                rec["ok"] = bool(okv)
                rec["reason"] = ("completed" if rec["ok"] else
                                 "no_documents" if not docs else "completion_proof_missing")
                if not rec["ok"] and not docs:
                    rec["error_raw"] = json.dumps(it, default=str)[:400]
            records.append(rec)
    records.sort(key=lambda r: r["manifest_index"])

    ok_n = sum(1 for r in records if r.get("ok"))
    chunks = sum(r.get("n_chunks") or 0 for r in records)
    with open(out / "per_doc.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps({
            "kind": "shot_meta", "arm": ARM, "posture": POSTURE,
            "mode": f"sharded_blast_inflight{INFLIGHT}" if INFLIGHT > 0 else "sharded_blast",
            "inflight_per_task": INFLIGHT,
            "ingestion_note": (f"{INFLIGHT} videos in flight per task (client-bounded), "
                               f"{K * INFLIGHT} box-wide" if INFLIGHT > 0 else
                               "one native batch per shard; engine admits up to "
                               f"threads={ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS} items per task "
                               f"(admission width {K * (ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS)} box-wide)"),
            "tasks": K, "n_docs": len(corpus), "ok_docs": ok_n,
            "span_s": round(span, 2),
            "measured_video_s": measured_video_s, "measured_source_s": measured_source_s,
            "measured_audio_s": measured_video_s,
            "realtime_factor": round(measured_video_s / span, 2) if span else None,
            "total_chunks": chunks, "timeout_s": TIMEOUT_S,
            "offered_concurrency": K * INFLIGHT if INFLIGHT > 0 else K,
            "offered_load_note": (f"{K} tasks x {INFLIGHT} videos in flight each, native "
                                  f"send_files per video, whole backlog offered — "
                                  if INFLIGHT > 0 else
                                  f"{K} parallel native batch calls (one per task) — ")
                                 + "native saturation interface, NOT numerically "
                                   "equivalent to LangGraph c32",
            "pipe_sha256": hashlib.sha256(PIPE_SRC.read_bytes()).hexdigest(),
            "provenance": {"pipeline_kind": "detect", "interval_s": 15,
                           "detect_model": "rfdetr", "threshold": 0.3,
                           "embed_model": "multi-qa-MiniLM-L6-cos-v1",
                           "split": {"chunk_size": 4000, "overlap": 0},
                           "expect_dim": 384, "bench_timeout_s": TIMEOUT_S,
                           "rr_pipe_ttl_s": TTL_S, "ttl_semantics": "0 = no expiry (verified in engine source)",
                           "threads_arg": ITEM_THREADS if ITEM_THREADS is not None else "OMITTED",
                           "engine_item_threads_expected_default": ENGINE_DEFAULT_ITEM_THREADS,
                           "engine_item_threads_effective": ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS,
                           "admission_width_box": K * (ITEM_THREADS or ENGINE_DEFAULT_ITEM_THREADS),
                           "admission_rule": "in-flight = connections x threads (engine team, #2053)",
                           "blas_omp_threads_per_task": BLAS_THREADS,
                           "torch_interop_per_task": "engine default (unverifiable)",
                           "duration_authority": "ffmpeg-probed video stream"},
            "threads_requested": ITEM_THREADS,
            "task_census": census,
            "warm_records": warm_records, "warm_docs": len(warm_set) * K,
            "idle_window_epoch_ns": [idle_start, idle_end],
            "event_actions_seen": dict(event_actions),
            "mono_offset_ns": mono_offset_ns,
            "measurement_start_epoch_ns": mono_offset_ns + t0,
            "measurement_end_epoch_ns": mono_offset_ns + t0 + span_ns,
            "envelope": "NONE — matched sizing run: all 32 vCPU, no cpuset; "
                        f"container memory limit per compose ({POSTURE})",
        }) + "\n")
    print(f"[rrm] DONE {POSTURE}: {ok_n}/{len(records)} ok, {chunks} chunks, "
          f"span={span:.1f}s ({measured_video_s/3600:.2f} h -> "
          f"{measured_video_s/span:.1f}x realtime)", flush=True)

    for c, t in zip(clients, tokens):
        try:
            await c.terminate(t)
        except Exception as exc:
            print(f"[rrm] terminate failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            await c.disconnect()
        except Exception:
            pass
    if ok_n < len(records):
        raise SystemExit(f"RUN INCOMPLETE: {len(records) - ok_n} docs not ok")


if __name__ == "__main__":
    asyncio.run(main())
