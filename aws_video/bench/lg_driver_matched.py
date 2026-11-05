"""LangGraph MATCHED-POSTURE driver — posture ``lg_matched_8x4_c32``.

Eight independent single-worker uvicorn processes (ports 8201..8208, one
process each — NOT --workers, whose kernel connection balancing skews),
fixed global client concurrency 32 = 8 endpoints x 4 active requests each,
on an EXPLICIT 32-thread executor. Videos go to endpoint index % 8 in
committed order. Separate from lg_driver.py (``lg_native_1process``).

Fail-closed before warm-up: all 8 /meta readbacks must show distinct PIDs,
torch threads == LG_TORCH_THREADS, interop == 1, six BLAS/OMP vars, models
loaded, detect concurrency == 1. Every endpoint is warmed with BOTH disjoint
fixtures; warm-up and an idle-burden window precede the measured span
(barrier -> last completion).

  python3 lg_driver_matched.py <corpus_dir> <out_dir> <n_docs> <warm_dir>
"""

import asyncio
import hashlib
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

HOST = os.environ.get("LG_HOST", "langgraph-matched")
PORT_BASE = int(os.environ.get("LG_PORT_BASE", "8201"))
W = int(os.environ.get("LG_WORKERS", "8"))
PER_EP = int(os.environ.get("LG_PER_ENDPOINT_CONCURRENCY", "4"))
GLOBAL_C = W * PER_EP
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "86400"))
TORCH_THREADS = int(os.environ.get("LG_TORCH_THREADS", "4"))
IDLE_S = int(os.environ.get("BENCH_IDLE_S", "30"))
# Expected predicts-at-once per process (readback gate). 1 = the 8-port matched
# cell; N>1 with W=1 = the in-process cell (one model copy, N concurrent predicts).
EXPECT_CONC = int(os.environ.get("LG_EXPECT_DETECT_CONC", "1"))
POSTURE = (f"lg_inproc_{EXPECT_CONC}x{TORCH_THREADS}_c{GLOBAL_C}" if W == 1
           else f"lg_matched_{W}x{TORCH_THREADS}_c{GLOBAL_C}")
ARM = "langgraph-video-detect-v1"
BLAS_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
             "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS")
ENDPOINTS = [f"http://{HOST}:{PORT_BASE + i}" for i in range(W)]


def post_video(base, video):
    with open(video, "rb") as fh:
        try:
            from requests_toolbelt import MultipartEncoder
            enc = MultipartEncoder(fields={"file": (video.name, fh, "video/mp4")})
            return requests.post(f"{base}/process", data=enc,
                                 headers={"Content-Type": enc.content_type}, timeout=TIMEOUT_S)
        except ImportError:
            return requests.post(f"{base}/process", files={"file": (video.name, fh)},
                                 timeout=TIMEOUT_S)


def process_one(base, ep, video, durations, extra):
    rec = base_record(video, durations)
    rec.update(extra)
    rec["endpoint_index"] = ep
    rec["endpoint"] = base
    rec["submit_ns"] = time.perf_counter_ns()
    try:
        r = post_video(base, video)
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


def wait_ready_all():
    for base in ENDPOINTS:
        for _ in range(300):
            try:
                if requests.get(f"{base}/health/ready", timeout=3).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise SystemExit(f"worker never ready: {base}")


def verify_readbacks():
    metas = []
    for base in ENDPOINTS:
        m = requests.get(f"{base}/meta", timeout=10).json()
        metas.append(m)
    problems = []
    pids = [m.get("pid") for m in metas]
    if len(set(pids)) != W or any(p is None for p in pids):
        problems.append(f"distinct worker pids: {pids}")
    for m in metas:
        rb = m.get("torch") or {}
        if rb.get("num_threads") != TORCH_THREADS:
            problems.append(f"pid {m.get('pid')}: torch threads {rb.get('num_threads')} != {TORCH_THREADS}")
        if rb.get("num_interop_threads") != 1:
            problems.append(f"pid {m.get('pid')}: interop {rb.get('num_interop_threads')} != 1")
        env = m.get("env") or {}
        bad = [v for v in BLAS_VARS if env.get(v) != str(TORCH_THREADS)]
        if bad:
            problems.append(f"pid {m.get('pid')}: env {bad} != {TORCH_THREADS}")
        if not (m.get("graph_compiled") and m.get("rfdetr_loaded") and m.get("minilm_loaded")):
            problems.append(f"pid {m.get('pid')}: models/graph not loaded {m}")
        if m.get("detect_concurrency_per_process") != EXPECT_CONC:
            problems.append(f"pid {m.get('pid')}: detect concurrency "
                            f"{m.get('detect_concurrency_per_process')} != {EXPECT_CONC}")
    if problems:
        raise SystemExit("WORKER READBACK FAIL: " + "; ".join(problems[:6]))
    ck = {m.get("rfdetr_checkpoint_sha256") for m in metas}
    if len(ck) != 1:
        raise SystemExit(f"CHECKPOINT MISMATCH across workers: {ck}")
    return metas


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
        raise SystemExit("matched posture requires disjoint warm fixtures in <warm_dir>")
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
         "endpoint_map": {v.name: i % W for i, v in enumerate(corpus)},
         "warm_fixtures": [w.name for w in warm_set]}))

    wait_ready_all()
    metas = verify_readbacks()
    print(f"[lgm] posture={POSTURE}: {W} workers verified (pids "
          f"{[m['pid'] for m in metas]}), torch {TORCH_THREADS}/1, detect concurrency 1", flush=True)

    # ---- warm every endpoint explicitly with BOTH fixtures (endpoints in
    # parallel, fixtures sequential per endpoint; excluded from measurement) ----
    warm_records = []
    def warm_endpoint(ep):
        out_ = []
        for wv in warm_set:
            rec = process_one(ENDPOINTS[ep], ep, wv, durations, {"warm": True})
            if not rec.get("ok"):
                raise SystemExit(f"WARM FAIL: endpoint {ep} ({ENDPOINTS[ep]}) on {wv.name}: "
                                 f"{rec.get('reason')} {rec.get('error')}")
            out_.append({"endpoint_index": ep, "worker_pid": rec.get("worker_pid"),
                         "fixture": wv.name,
                         "warm_s": round((rec['completion_ns'] - rec['submit_ns']) / 1e9, 1)})
        return out_
    with ThreadPoolExecutor(max_workers=W) as wex:
        for out_ in wex.map(warm_endpoint, range(W)):
            warm_records.extend(out_)
    pids_warm = {r["worker_pid"] for r in warm_records}
    if len(pids_warm) != W:
        raise SystemExit(f"WARM FAIL: warm requests served by {len(pids_warm)} distinct pids, expected {W}")
    print(f"[lgm] warm-up: all {W} endpoints warmed on {len(warm_set)} fixtures (excluded)", flush=True)

    mono_offset_ns = time.time_ns() - time.perf_counter_ns()
    idle_start = time.time_ns()
    await asyncio.sleep(IDLE_S)
    idle_end = time.time_ns()

    # ---- fixed c32: explicit 32-thread executor, 4 active per endpoint, barrier ----
    executor = ThreadPoolExecutor(max_workers=GLOBAL_C)
    ep_sem = [threading.Semaphore(PER_EP) for _ in range(W)]
    inflight_lock = threading.Lock()
    inflight = [0] * W
    obs = {"max_global": 0, "max_per_endpoint": [0] * W}
    barrier = threading.Event()
    progress_fh = open(out / "progress.jsonl", "a", buffering=1)
    done = [0]

    def run_one(i, video):
        ep = i % W
        barrier.wait()
        with ep_sem[ep]:
            with inflight_lock:
                inflight[ep] += 1
                g = sum(inflight)
                obs["max_global"] = max(obs["max_global"], g)
                obs["max_per_endpoint"][ep] = max(obs["max_per_endpoint"][ep], inflight[ep])
            try:
                rec = process_one(ENDPOINTS[ep], ep, video, durations,
                                  {"manifest_index": i, "shard_local_index": i // W})
            finally:
                with inflight_lock:
                    inflight[ep] -= 1
        done[0] += 1
        progress_fh.write(json.dumps({"doc": video.name, "action": rec["reason"],
                                      "endpoint_index": ep,
                                      "t_rel_s": round((time.perf_counter_ns() - t0) / 1e9, 1),
                                      "n_done": done[0]}) + "\n")
        print(f"[lgm] {done[0]}/{len(corpus)} {video.name} ep{ep} {rec['reason']}", flush=True)
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
            "configured_per_endpoint_concurrency": PER_EP,
            "observed_max_global_requests": obs["max_global"],
            "observed_max_requests_per_endpoint": obs["max_per_endpoint"],
            "worker_readbacks": metas,
            "provenance": {"pipeline_kind": "detect", "interval_s": 15,
                           "detect_model": "rfdetr", "threshold": 0.3,
                           "embed_model": "multi-qa-MiniLM-L6-cos-v1",
                           "split": {"chunk_size": 4000, "overlap": 0},
                           "expect_dim": 384, "bench_timeout_s": TIMEOUT_S,
                           "torch_threads_per_process": TORCH_THREADS,
                           "torch_interop_per_process": 1,
                           "detect_concurrency_per_process": EXPECT_CONC,
                           "model_copies": W,
                           "duration_authority": "ffmpeg-probed video stream",
                           "service_meta": metas[0]},
            "warm_records": warm_records, "warm_docs": len(warm_set) * W,
            "idle_window_epoch_ns": [idle_start, idle_end],
            "mono_offset_ns": mono_offset_ns,
            "measurement_start_epoch_ns": mono_offset_ns + t0,
            "measurement_end_epoch_ns": mono_offset_ns + t0 + span_ns,
            "envelope": f"NONE — matched sizing run: all 32 vCPU, no cpuset ({POSTURE})",
        }) + "\n")
    print(f"[lgm] DONE {POSTURE}: {ok_n}/{len(records)} ok, {chunks} chunks, span={span:.1f}s "
          f"({measured_video_s/span:.1f}x realtime); observed max global {obs['max_global']}, "
          f"per-endpoint {obs['max_per_endpoint']}", flush=True)
    if ok_n < len(records):
        raise SystemExit(f"RUN INCOMPLETE: {len(records) - ok_n} docs not ok")


if __name__ == "__main__":
    asyncio.run(main())
