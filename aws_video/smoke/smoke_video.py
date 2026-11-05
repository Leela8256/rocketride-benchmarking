"""One-video smoke for the RocketRide VIDEO pipeline.

Sends the SAME video through pipe/benchmark_video.pipe REPS times on one
pipeline instance and turns the responses into explicit verdicts for the
risks the video pipe carries (none of which the PDF benchmark ever tested):

  R1 lane routing      webhook must put an .avi on the `video` lane at all
  R2 audio demux+ASR   faster-whisper must read PCM-in-AVI and emit speech text
  R3 frame lane        frame_grabber -> detect must contribute chunks too
  R4 structure         384-dim finite vectors, L2 norm ~= 1 (same gate as PDF)
  R5 content determinism   same chunk-hash MULTISET across reps
  R6 order determinism     same chunk-hash SEQUENCE across reps

R1/R2/R4/R5 failing fails the smoke (exit non-zero). R3 and R6 are WARN:
R3 because chunk->lane provenance may not be visible in the response, and
R6 because a two-producer merge may interleave nondeterministically even
when content is stable — that outcome would mean the benchmark determinism
gate needs a sorted comparison, which is a finding, not a failure.

Rep 1 on a cold engine includes pip-installing the whisper/rfdetr stacks and
downloading model weights; its wall time is init+work and is never a number
to quote. Rep 2 is the honest single-doc service time.

  python smoke_video.py <video_path> <out_dir> [reps=2]
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_video.pipe"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "5400"))
USE_TIMEOUT_S = int(os.environ.get("BENCH_USE_TIMEOUT_S", "2700"))
EMBED_DIM = 384


def documents_from(result):
    """Unwrap the engine's nested response shapes. Same logic as
    aws_pdf/bench/rr_driver.py — do not simplify without re-checking a raw
    capture; the nesting varies by node."""
    if isinstance(result, dict):
        docs = result.get("documents")
        if isinstance(docs, list) and all(isinstance(d, dict) for d in docs):
            return docs
        for k in ("result", "data", "output"):
            if k in result:
                got = documents_from(result[k])
                if got:
                    return got
        return []
    if isinstance(result, list):
        out = []
        for item in result:
            out.extend(documents_from(item))
        return out
    return []


def looks_like_detection(text):
    """Heuristic fallback when metadata carries no lane provenance: detector
    output is label/number dense, speech is not."""
    if not text:
        return False
    t = text.lower()
    if "person" in t and any(ch in t for ch in "[]{}():"):
        return True
    hard = sum(c.isdigit() or c in "[]{}():,." for c in text)
    return hard / max(len(text), 1) > 0.25


def summarize(docs):
    texts = [d.get("page_content", "") for d in docs]
    hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    norms, dim_ok = [], True
    for d in docs:
        v = d.get("embedding") or []
        if len(v) != EMBED_DIM or not all(x == x and abs(x) != float("inf") for x in v):
            dim_ok = False
        norms.append(sum(x * x for x in v) ** 0.5)
    meta_keys = Counter()
    for d in docs:
        m = d.get("metadata")
        if isinstance(m, dict):
            meta_keys.update(m.keys())
    return {
        "n_chunks": len(docs),
        "total_chars": sum(len(t) for t in texts),
        "chunk_sha256": hashes,
        "structure_ok": dim_ok and bool(docs),
        "l2_norms_minmax": [round(min(norms), 6), round(max(norms), 6)] if norms else None,
        "metadata_keys_seen": dict(meta_keys),
        "n_detection_like": sum(1 for t in texts if looks_like_detection(t)),
        "n_speech_like": sum(1 for t in texts if t and not looks_like_detection(t)),
    }


async def heartbeat(label, t0):
    while True:
        await asyncio.sleep(60)
        print(f"[smoke] ... {label} still running, {time.monotonic() - t0:.0f}s elapsed",
              flush=True)


async def timed(label, coro, timeout):
    t0 = time.monotonic()
    hb = asyncio.create_task(heartbeat(label, t0))
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
    finally:
        hb.cancel()
    dt = time.monotonic() - t0
    print(f"[smoke] {label} done in {dt:.1f}s", flush=True)
    return result, dt


async def main():
    from rocketride import RocketRideClient

    video = Path(sys.argv[1])
    out = Path(sys.argv[2])
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    out.mkdir(parents=True, exist_ok=True)
    assert video.is_file(), f"no such video: {video}"

    pipe = json.loads(PIPE_SRC.read_text())
    pipe["project_id"] = str(uuid.uuid4())
    pipe_path = out / "pipeline.pipe"
    pipe_path.write_text(json.dumps(pipe))

    print(f"[smoke] video={video.name} size={video.stat().st_size:,}B "
          f"reps={reps} uri={URI}", flush=True)

    client = RocketRideClient(uri=URI, auth=APIKEY)
    await client.connect()
    # Rep 1 of a cold engine pays pip installs + model downloads somewhere
    # between use() and the first send — both get generous timeouts.
    used, use_s = await timed(
        "use(pipeline)",
        client.use(filepath=str(pipe_path), use_existing=True, ttl=7200),
        USE_TIMEOUT_S)
    token = used["token"]
    print(f"[smoke] pipeline up, token={token}", flush=True)

    rep_summaries = []
    try:
        for r in range(1, reps + 1):
            resp, span = await timed(
                f"rep{r} send_files({video.name})",
                client.send_files([(str(video), {"doc_id": f"rep{r}-{video.stem}"})],
                                  token),
                TIMEOUT_S)
            docs = documents_from(resp)
            s = summarize(docs)
            s["rep"] = r
            s["span_s"] = round(span, 1)
            rep_summaries.append(s)
            # Full text + metadata per chunk (embedding reduced to its norm)
            # so lane provenance and any nondeterminism can be diffed offline.
            (out / f"docs_rep{r}.json").write_text(json.dumps([
                {"page_content": d.get("page_content", ""),
                 "metadata": d.get("metadata"),
                 "l2_norm": round(sum(x * x for x in (d.get("embedding") or [])) ** 0.5, 6)}
                for d in docs], indent=1))
            if not docs:
                (out / f"raw_rep{r}.json").write_text(
                    json.dumps(resp, default=str)[:200000])
            print(f"[smoke] rep{r}: chunks={s['n_chunks']} chars={s['total_chars']} "
                  f"structure_ok={s['structure_ok']} "
                  f"speech_like={s['n_speech_like']} detection_like={s['n_detection_like']}",
                  flush=True)
    finally:
        try:
            await client.terminate(token)
        except Exception as exc:
            print(f"[smoke] terminate failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            await client.disconnect()
        except Exception:
            pass

    # ---- verdicts ----------------------------------------------------------
    r1 = rep_summaries[0]
    routed = r1["n_chunks"] > 0
    speech = r1["n_speech_like"] > 0 and r1["total_chars"] > 2000
    frames = r1["n_detection_like"] > 0
    structure = all(s["structure_ok"] for s in rep_summaries)
    content_det = order_det = None
    if len(rep_summaries) >= 2:
        a, b = rep_summaries[0]["chunk_sha256"], rep_summaries[1]["chunk_sha256"]
        content_det = sorted(a) == sorted(b)
        order_det = a == b

    def v(flag, warn=False):
        if flag is None:
            return "UNTESTED"
        return ("PASS" if flag else ("WARN" if warn else "FAIL"))

    verdicts = {
        "R1_video_lane_routing": v(routed),
        "R2_audio_demux_and_asr": v(speech),
        "R3_frame_lane_contributes": v(frames, warn=True),
        "R4_structure_384_finite_norm1": v(structure),
        "R5_content_determinism": v(content_det),
        "R6_order_determinism": v(order_det, warn=True),
    }
    result = {
        "video": video.name,
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "video_bytes": video.stat().st_size,
        "pipe": PIPE_SRC.name,
        "pipe_sha256": hashlib.sha256(PIPE_SRC.read_bytes()).hexdigest(),
        "engine": "rocketride-3.3.1 + dup-patch (see engine/Dockerfile)",
        "use_s": round(use_s, 1),
        "reps": [{k: s[k] for k in
                  ("rep", "span_s", "n_chunks", "total_chars", "structure_ok",
                   "l2_norms_minmax", "n_speech_like", "n_detection_like",
                   "metadata_keys_seen")}
                 for s in rep_summaries],
        "verdicts": verdicts,
        "note_rep1": "rep1 span includes one-time engine init on a cold cache; "
                     "never quote it as service time",
    }
    (out / "smoke_result.json").write_text(json.dumps(result, indent=1))

    print("\n========== SMOKE VERDICTS ==========", flush=True)
    for k, val in verdicts.items():
        print(f"  {k:34s} {val}", flush=True)
    spans = ", ".join(f"rep{s['rep']}={s['span_s']}s" for s in rep_summaries)
    print(f"  spans: {spans}   (use()={use_s:.1f}s)", flush=True)
    print(f"  full result: {out / 'smoke_result.json'}", flush=True)

    hard_fail = [k for k, val in verdicts.items()
                 if val == "FAIL" and k not in
                 ("R3_frame_lane_contributes", "R6_order_determinism")]
    if hard_fail:
        raise SystemExit(f"SMOKE FAILED: {hard_fail}")
    print("SMOKE PASSED (warnings above, if any, are findings to carry "
          "into the benchmark design)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
