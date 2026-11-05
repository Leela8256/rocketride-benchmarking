"""LangGraph ONE-PORT multi-worker driver — posture ``lg_workers8_1port_c32``.

The "obvious" deployment: ``uvicorn --workers 8`` on ONE port (8200). Eight
worker processes share the socket and the KERNEL picks which one answers
each connection — the client cannot address, warm or verify a specific
worker. Same per-worker settings as the matched cell (torch 4/1, one
predict at a time per worker, BLAS/OMP = 4) so the only difference from
``lg_matched_8x4_c32`` is the port layout / load balancing.

Fail-closed, as far as one port allows:
  - readback: /meta is polled until W distinct worker pids have answered
    (cap 200 polls); every answer must show torch == LG_TORCH_THREADS,
    interop == 1, six env vars, models loaded, detect concurrency 1,
    identical checkpoint hash
  - warm-up: the two disjoint fixtures are re-sent until every one of the
    W pids has served at least one warm request (cap 12 rounds) —
    "re-sent until every worker has served" (Ansh's protocol)
  - c32: explicit 32-thread executor, global semaphore 32, barrier
  - per-record worker_pid -> the report shows how the kernel spread the load

  python3 lg_driver_1port.py <corpus_dir> <out_dir> <n_docs> <warm_dir>
"""

import asyncio
import collections
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from lg_driver import base_record, fill_from_response, VIDEO_DURS   # noqa: E402
from lg_driver_matched import post_video, BLAS_VARS                  # noqa: E402

BASE = os.environ.get("LG_URL", "http://langgraph-workers8:8200")
W = int(os.environ.get("LG_WORKERS", "8"))
GLOBAL_C = int(os.environ.get("LG_GLOBAL_CONCURRENCY", "32"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "86400"))
TORCH_THREADS = int(os.environ.get("LG_TORCH_THREADS", "4"))
IDLE_S = int(os.environ.get("BENCH_IDLE_S", "30"))
POSTURE = f"lg_workers{W}_1port_c{GLOBAL_C}"
ARM = "langgraph-video-detect-v1"


def process_one(video, durations, extra):
    rec = base_record(video, durations)
    rec.update(extra)
    rec["endpoint"] = BASE
    rec["submit_ns"] = time.perf_counter_ns()
    try:
        r = post_video(BASE, video)
        rec["completion_ns"] = time.perf_counter_ns()
        rec["timing_source"] = "client-observed HTTP response"
        if r.status_code != 200:
            rec["reason"] = "service_error"
            rec["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            rec["identity_ok"] = False
            return rec
        out = r.json()
        rec["worker_pid"] = out.get("worker_pid")
        rec["worker_port"] = out.get("port")
        rec["identity_ok"] = out.get("filename") == video.name
        okv = fill_from_response(rec, out)
        rec["ok"] = bool(okv and rec["identity_ok"])
        rec["reason"] = ("completed" if rec["ok"] else
                         "no_documents" if not out.get("documents") else "completion_proof_missing")
    except requests.Timeout:
        rec["completion_ns"] = time.perf_counter_ns()
        rec["reason"] = "timeout"
    except Exception as exc:
        rec["completion_ns"] = time.perf_counter_ns()
        rec["reason"] = "transport_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:250]
    return rec


def wait_ready():
    for _ in range(300):
        try:
            if requests.get(f"{BASE}/health/ready", timeout=3).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"service never ready: {BASE}")


def verify_readbacks():
    """Poll /meta until W distinct pids have answered; validate every answer."""
    metas, problems = {}, []
    for _ in range(200):
        m = requests.get(f"{BASE}/meta", timeout=10).json()
        pid = m.get("pid")
        if pid is not None and pid not in metas:
            metas[pid] = m
            rb = m.get("torch") or {}
            if rb.get("num_threads") != TORCH_THREADS:
                problems.append(f"pid {pid}: torch threads {rb.get('num_threads')} != {TORCH_THREADS}")
            if rb.get("num_interop_threads") != 1:
                problems.append(f"pid {pid}: interop {rb.get('num_interop_threads')} != 1")
            env = m.get("env") or {}
            bad = [v for v in BLAS_VARS if env.get(v) != str(TORCH_THREADS)]
            if bad:
                problems.append(f"pid {pid}: env {bad} != {TORCH_THREADS}")
            if not (m.get("graph_compiled") and m.get("rfdetr_loaded") and m.get("minilm_loaded")):
                problems.append(f"pid {pid}: models/graph not loaded")
            if m.get("detect_concurrency_per_process") != 1:
                problems.append(f"pid {pid}: detect concurrency {m.get('detect_concurrency_per_process')} != 1")
        if len(metas) >= W:
            break
        time.sleep(0.2)
    if len(metas) != W:
        raise SystemExit(f"WORKER READBACK FAIL: saw {len(metas)} distinct worker pids on one port "
                         f"after 200 polls, expected {W} (kernel never routed to some workers?)")
    if problems:
        raise SystemExit("WORKER READBACK FAIL: " + "; ".join(problems[:6]))
    ck = {m.get("rfdetr_checkpoint_sha256") for m in metas.values()}
    if len(ck) != 1:
        raise SystemExit(f"CHECKPOINT MISMATCH across workers: {ck}")
    return list(metas.values())


async def main():
    corpus_dir, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    warm_dir = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    order_file = corpus_dir / "measured_order.txt"
    if order_file.exists():
        names = [l.strip() for l in open(order_file) if l.strip() and not l.startswith("#")]
        corpus = [corpus_dir / nm for nm in names][:n]
    else:
        corpus = sorted(list(corpus_dir.glob("*.avi")) + list(corpus_dir.glob("*.mp4")))[:n]
    if len(corpus) != n:
        raise SystemExit(f"requested {n} measured docs, found {len(corpus)}")
    warm_set = sorted(list(warm_dir.glob("*.avi")) + list(warm_dir.glob("*.mp4"))) if warm_dir else []
    if not warm_set:
        raise SystemExit("this posture requires disjoint warm fixtures in <warm_dir>")
    if set(v.name for v in warm_set) & set(v.name for v in corpus):
        raise SystemExit("warm fixtures overlap the measured corpus")
    out.mkdir(parents=True, exist_ok=True)

    durations, corpus_shas, video_durs = {}, {}, {}
    mf = corpus_dir / "corpus_manifest.json"
    if mf.exists():
        _m = json.loads(mf.read_text())
        durations = _m.get("duration_s", {})
        corpus_shas = _m.get("sha256", {})
        video_durs = _m.get("video_duration_s", {})
        VIDEO_DURS.update(video_durs)
    measured_video_s = sum(video_durs.get(v.name) or durations.get(v.name) or 0 for v in corpus)
    measured_source_s = sum(durations.get(v.name) or 0 for v in corpus)
    (out / "manifest.json").write_text(json.dumps(
        {"docs": [v.name for v in corpus], "n": len(corpus), "posture": POSTURE,
         "measured_video_s": measured_video_s, "measured_source_s": measured_source_s,
         "sha256": {v.name: corpus_shas[v.name] for v in corpus if v.name in corpus_shas},
         "video_duration_s": {v.name: video_durs[v.name] for v in corpus if v.name in video_durs},
         "warm_fixtures": [w.name for w in warm_set]}))

    wait_ready()
    metas = verify_readbacks()
    pids = sorted(m["pid"] for m in metas)
    print(f"[lg1] posture={POSTURE}: {W} distinct workers seen on one port (pids {pids}), "
          f"torch {TORCH_THREADS}/1, detect concurrency 1", flush=True)

    # ---- warm-up: re-send the fixtures until every worker pid has served ----
    warm_records, served = [], set()
    for rnd in range(12):
        with ThreadPoolExecutor(max_workers=GLOBAL_C) as wex:
            recs = list(wex.map(lambda wv: process_one(wv, durations, {"warm": True, "round": rnd}),
                                [wv for wv in warm_set for _ in range(W)]))
        for rec in recs:
            if not rec.get("ok"):
                raise SystemExit(f"WARM FAIL: {rec.get('reason')} {rec.get('error')}")
            served.add(rec.get("worker_pid"))
            warm_records.append({"round": rnd, "worker_pid": rec.get("worker_pid"),
                                 "fixture": rec["doc"],
                                 "warm_s": round((rec["completion_ns"] - rec["submit_ns"]) / 1e9, 1)})
        if served >= set(pids):
            break
    if not served >= set(pids):
        raise SystemExit(f"WARM FAIL: after 12 rounds only {len(served)}/{W} workers served a warm request")
    print(f"[lg1] warm-up: all {W} workers served ({len(warm_records)} warm requests, "
          f"{rnd + 1} round(s)); excluded", flush=True)

    mono_offset_ns = time.time_ns() - time.perf_counter_ns()
    idle_start = time.time_ns()
    await asyncio.sleep(IDLE_S)
    idle_end = time.time_ns()

    executor = ThreadPoolExecutor(max_workers=GLOBAL_C)
    gsem = threading.Semaphore(GLOBAL_C)
    lock = threading.Lock()
    inflight = [0]
    obs = {"max_global": 0}
    barrier = threading.Event()
    progress_fh = open(out / "progress.jsonl", "a", buffering=1)
    done = [0]

    def run_one(i, video):
        barrier.wait()
        with gsem:
            with lock:
                inflight[0] += 1
                obs["max_global"] = max(obs["max_global"], inflight[0])
            try:
                rec = process_one(video, durations, {"manifest_index": i})
            finally:
                with lock:
                    inflight[0] -= 1
        done[0] += 1
        progress_fh.write(json.dumps({"doc": video.name, "action": rec["reason"],
                                      "worker_pid": rec.get("worker_pid"),
                                      "t_rel_s": round((time.perf_counter_ns() - t0) / 1e9, 1),
                                      "n_done": done[0]}) + "\n")
        print(f"[lg1] {done[0]}/{len(corpus)} {video.name} pid={rec.get('worker_pid')} {rec['reason']}", flush=True)
        return rec

    loop = asyncio.get_running_loop()
    futs = [loop.run_in_executor(executor, run_one, i, v) for i, v in enumerate(corpus)]
    t0 = time.perf_counter_ns()
    barrier.set()
    records = list(await asyncio.gather(*futs))
    span_ns = time.perf_counter_ns() - t0
    span = span_ns / 1e9
    executor.shutdown(wait=True)
    records.sort(key=lambda r: r["manifest_index"])

    ok_n = sum(1 for r in records if r.get("ok"))
    chunks = sum(r.get("n_chunks") or 0 for r in records)
    per_worker = collections.Counter(r.get("worker_pid") for r in records)
    with open(out / "per_doc.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps({
            "kind": "shot_meta", "arm": ARM, "posture": POSTURE, "mode": f"c{GLOBAL_C}",
            "workers": W, "n_docs": len(corpus), "ok_docs": ok_n,
            "span_s": round(span, 2),
            "measured_video_s": measured_video_s, "measured_source_s": measured_source_s,
            "measured_audio_s": measured_video_s,
            "realtime_factor": round(measured_video_s / span, 2) if span else None,
            "total_chunks": chunks, "timeout_s": TIMEOUT_S,
            "offered_concurrency": GLOBAL_C,
            "configured_global_concurrency": GLOBAL_C,
            "observed_max_global_requests": obs["max_global"],
            "videos_per_worker_pid": {str(k): v for k, v in sorted(per_worker.items(), key=lambda kv: -kv[1])},
            "load_balancing": "kernel accept() on one shared port — not client-controlled",
            "worker_readbacks": metas,
            "provenance": {"pipeline_kind": "detect", "interval_s": 15,
                           "detect_model": "rfdetr", "threshold": 0.3,
                           "embed_model": "multi-qa-MiniLM-L6-cos-v1",
                           "split": {"chunk_size": 4000, "overlap": 0},
                           "expect_dim": 384, "bench_timeout_s": TIMEOUT_S,
                           "torch_threads_per_process": TORCH_THREADS,
                           "torch_interop_per_process": 1,
                           "detect_concurrency_per_process": 1,
                           "duration_authority": "ffmpeg-probed video stream",
                           "service_meta": metas[0]},
            "warm_records": warm_records, "warm_docs": len(warm_records),
            "idle_window_epoch_ns": [idle_start, idle_end],
            "mono_offset_ns": mono_offset_ns,
            "measurement_start_epoch_ns": mono_offset_ns + t0,
            "measurement_end_epoch_ns": mono_offset_ns + t0 + span_ns,
            "envelope": f"NONE — sizing run: all 32 vCPU, no cpuset ({POSTURE})",
        }) + "\n")
    print(f"[lg1] DONE {POSTURE}: {ok_n}/{len(records)} ok, {chunks} chunks, span={span:.1f}s "
          f"({measured_video_s/span:.1f}x realtime); videos per worker pid: "
          f"{dict(per_worker.most_common())}", flush=True)
    if ok_n < len(records):
        raise SystemExit(f"RUN INCOMPLETE: {len(records) - ok_n} docs not ok")


if __name__ == "__main__":
    asyncio.run(main())
