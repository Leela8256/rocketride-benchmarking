"""LangGraph VIDEO driver — writes the SAME per_doc.jsonl schema as
bench_video.py so bench/report.py treats both arms identically.

LangGraph is an HTTP service, so its native ingestion is per-video
requests; modes are seq and c<N> (there is no batch API to blast — that
asymmetry is the same one the PDF bench documents for native modes).

  python3 lg_driver.py <corpus_dir> <out_dir> <n_docs> [mode] [warm_docs]
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("LG_URL", "http://langgraph:8200")
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "86400"))
EMBED_DIM = 384
ARM = "langgraph-video-detect-v1"
VIDEO_DURS: dict = {}


def base_record(video, durations):
    return {"doc": video.name, "arm": ARM, "ok": False,
            "input_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "size_bytes": video.stat().st_size,
            "duration_s": durations.get(video.name),
            "video_duration_s": VIDEO_DURS.get(video.name)}


def embedding_digest(docs):
    import struct
    h = hashlib.sha256()
    for d in docs:
        v = d.get("embedding") or []
        h.update(struct.pack(f"<{len(v)}d", *v))
    return h.hexdigest()


def fill_from_response(rec, out):
    docs = out.get("documents", [])
    texts = [d.get("page_content", "") for d in docs]
    okv = bool(docs) and all(
        len(d.get("embedding") or []) == EMBED_DIM and
        all(x == x and abs(x) != float("inf") for x in d["embedding"])
        for d in docs)
    rec["n_chunks"] = len(docs)
    rec["total_chars"] = sum(len(t) for t in texts)
    full = "".join(texts)
    rec["n_frames_est"] = full.count("[{") + full.count("[]")
    rec["n_detections"] = full.count('"label"')
    rec["chunk_sha256"] = [hashlib.sha256(t.encode("utf-8")).hexdigest()
                           for t in texts]
    rec["vector_dim"] = EMBED_DIM if okv else None
    norms = [sum(x * x for x in (d.get("embedding") or [])) ** 0.5 for d in docs]
    rec["l2_norms_minmax"] = ([round(min(norms), 6), round(max(norms), 6)]
                              if norms else None)
    rec["embedding_sha256"] = embedding_digest(docs) if docs else None
    rec["lg_timings"] = out.get("timings")     # per-node decomposition (V4)
    rec["lg_n_frames_reported"] = out.get("n_frames")
    return okv


def _post_video(video):
    """Stream the multipart body from disk. requests' files= builds the
    whole body in memory — 32 in-flight ~0.7 GB films held ~20 GB in the
    DRIVER container and helped OOM the box (films500 post-mortem
    2026-08-24). requests-toolbelt streams; fall back with a warning."""
    with open(video, "rb") as fh:
        try:
            from requests_toolbelt import MultipartEncoder
            enc = MultipartEncoder(fields={"file": (video.name, fh, "video/mp4")})
            return requests.post(f"{BASE}/process", data=enc,
                                 headers={"Content-Type": enc.content_type},
                                 timeout=TIMEOUT_S)
        except ImportError:
            print("[lgv] WARNING: requests_toolbelt missing — buffering upload "
                  "in memory", flush=True)
            return requests.post(f"{BASE}/process",
                                 files={"file": (video.name, fh)},
                                 timeout=TIMEOUT_S)


def process_one(video, durations):
    rec = base_record(video, durations)
    rec["submit_ns"] = time.perf_counter_ns()
    try:
        r = _post_video(video)
        rec["completion_ns"] = time.perf_counter_ns()
        rec["timing_source"] = "client-observed HTTP response"
        if r.status_code != 200:
            rec["reason"] = "http_error"
            rec["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            rec["identity_ok"] = False
            return rec
        out = r.json()
        rec["identity_ok"] = out.get("filename") == video.name
        okv = fill_from_response(rec, out)
        rec["ok"] = bool(okv and rec["identity_ok"])
        rec["reason"] = ("completed" if rec["ok"] else
                         "no_documents" if not out.get("documents") else
                         "completion_proof_missing")
    except requests.Timeout:
        rec["completion_ns"] = time.perf_counter_ns()
        rec["reason"] = "timeout"
    except Exception as exc:
        rec["completion_ns"] = time.perf_counter_ns()
        rec["reason"] = "transport_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:250]
    return rec


async def main():
    corpus_dir, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "seq"
    warm_docs = int(sys.argv[5]) if len(sys.argv) > 5 else 0

    all_videos = sorted(list(corpus_dir.glob("*.avi"))
                     + list(corpus_dir.glob("*.mp4")))
    corpus = all_videos[:n]
    if not corpus:
        raise SystemExit(f"no videos in {corpus_dir}")
    out.mkdir(parents=True, exist_ok=True)
    durations, corpus_shas, video_durs = {}, {}, {}
    mf = corpus_dir / "corpus_manifest.json"
    if mf.exists():
        _m = json.loads(mf.read_text())
        durations = _m.get("duration_s", {})
        corpus_shas = _m.get("sha256", {})
        video_durs = _m.get("video_duration_s", {})
        VIDEO_DURS.update(video_durs)
    # Probed video-stream duration is the footage denominator (2026-08-23) —
    # same authority rule as bench_video.py; source metadata kept beside it.
    measured_video_s = sum(video_durs.get(v.name) or durations.get(v.name) or 0
                           for v in corpus)
    measured_source_s = sum(durations.get(v.name) or 0 for v in corpus)
    (out / "manifest.json").write_text(json.dumps(
        {"docs": [v.name for v in corpus], "n": len(corpus),
         "measured_video_s": measured_video_s,
         "measured_source_s": measured_source_s,
         # sha map from the corpus manifest -> the corpus_pin gate verifies
         "sha256": {v.name: corpus_shas[v.name]
                    for v in corpus if v.name in corpus_shas},
         "video_duration_s": {v.name: video_durs[v.name]
                              for v in corpus if v.name in video_durs}}))

    if mode == "seq":
        offered = 1
    elif mode.startswith("c") and mode[1:].isdigit():
        offered = int(mode[1:])
    else:
        raise SystemExit(f"bad mode {mode!r}: LangGraph modes are seq | c<N>")

    for _ in range(300):
        try:
            if requests.get(f"{BASE}/health/ready", timeout=3).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise SystemExit("LangGraph service never became ready")
    meta = requests.get(f"{BASE}/meta", timeout=5).json()
    print(f"[lgv] service ready: {meta}", flush=True)
    print(f"[lgv] corpus={len(corpus)} docs, {measured_video_s/3600:.2f} h "
          f"(video-stream probed), mode={mode}, warm={warm_docs}", flush=True)

    warm_set = all_videos[n:n + warm_docs]
    warm_s = None
    if warm_set:
        tw = time.perf_counter_ns()
        for v in warm_set:
            process_one(v, durations)
        warm_s = (time.perf_counter_ns() - tw) / 1e9
        print(f"[lgv] warm-start: {len(warm_set)} docs in {warm_s:.1f}s "
              f"(excluded)", flush=True)

    progress_fh = open(out / "progress.jsonl", "a", buffering=1)
    done = [0]
    sem = asyncio.Semaphore(offered)
    # Epoch<->monotonic mapping so the report can window the cgroup sampler
    # (epoch-stamped) to the measured interval — LG previously had no such
    # anchor and its warm-up CPU could not be excluded post hoc.
    mono_offset_ns = time.time_ns() - time.perf_counter_ns()
    t0 = time.perf_counter_ns()

    async def one(video):
        async with sem:
            rec = await asyncio.to_thread(process_one, video, durations)
            done[0] += 1
            progress_fh.write(json.dumps(
                {"doc": video.name, "action": rec["reason"],
                 "t_rel_s": round((time.perf_counter_ns() - t0) / 1e9, 1),
                 "n_done": done[0]}) + "\n")
            print(f"[lgv] {done[0]}/{len(corpus)} {video.name} {rec['reason']}",
                  flush=True)
            return rec

    records = list(await asyncio.gather(*[one(v) for v in corpus]))
    span = (time.perf_counter_ns() - t0) / 1e9

    ok_n = sum(1 for r in records if r.get("ok"))
    chunks = sum(r.get("n_chunks") or 0 for r in records)
    with open(out / "per_doc.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps({
            "kind": "shot_meta", "arm": ARM, "mode": mode,
            "n_docs": len(corpus), "ok_docs": ok_n,
            "span_s": round(span, 2),
            "measured_video_s": measured_video_s,
            "measured_source_s": measured_source_s,
            # legacy alias — carries the probed denominator (2026-08-23)
            "measured_audio_s": measured_video_s,
            "realtime_factor": round(measured_video_s / span, 2) if span else None,
            "total_chunks": chunks,
            "timeout_s": TIMEOUT_S,
            "offered_concurrency": offered,
            "provenance": {"pipeline_kind": "detect", "interval_s": 15,
                           "detect_model": "rfdetr", "threshold": 0.3,
                           "embed_model": "multi-qa-MiniLM-L6-cos-v1",
                           "split": {"chunk_size": 4000, "overlap": 0},
                           "expect_dim": 384, "service_meta": meta,
                           "bench_timeout_s": TIMEOUT_S,
                           "duration_authority": "ffmpeg-probed video stream"},
            "warm_docs": len(warm_set), "warm_s": warm_s,
            "mono_offset_ns": mono_offset_ns,
            "measurement_start_epoch_ns": mono_offset_ns + t0,
            "measurement_end_epoch_ns": mono_offset_ns + t0 + int(span * 1e9),
            "envelope": os.environ.get("BENCH_ENVELOPE",
                                       "NONE — sizing run: no cpuset, threads unpinned"),
        }) + "\n")
    print(f"[lgv] DONE mode={mode}: {ok_n}/{len(records)} ok, {chunks} chunks, "
          f"span={span:.1f}s ({measured_video_s/span:.1f}x realtime)", flush=True)
    if ok_n < len(records):
        raise SystemExit(f"RUN INCOMPLETE: {len(records) - ok_n} docs not ok")


if __name__ == "__main__":
    asyncio.run(main())
