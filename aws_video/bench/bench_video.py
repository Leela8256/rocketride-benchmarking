"""RocketRide VIDEO batch driver — sizing runs (adapted from aws_pdf/bench/rr_driver.py).

Same record schema and response-unwrapping as the PDF driver; differences:
  - corpus is *.avi; per-doc audio duration joined from corpus_manifest.json,
    so throughput can be quoted as MEETING-MINUTES PER SECOND (x realtime),
    the unit that answers "how long will N hours of footage take".
  - default mode is blast (ONE send_files batch — RocketRide's native path).
  - this is a SIZING run, not a gated benchmark: no envelope, single rep,
    the R5 whisper nondeterminism finding stands and is irrelevant here.

RR_TRACE=metadata|summary|full turns on per-node timing from the engine's own
apaevt_flow events (off by default — the control for a traced run is this same
driver with RR_TRACE unset). Output: flow_events.jsonl + node_timings.json,
and trace_level/flow_ops_seen/node_timings in shot_meta. See flow_selftime.py
for why the numbers are EXCLUSIVE self time and not leave-minus-enter.

  python3 bench_video.py <corpus_dir> <out_dir> <n_docs> [mode] [warm_docs]
"""

import asyncio
import collections
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_video.pipe"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "86400"))
# Engine pipeline TTL must outlive the whole batch: a films500 blast runs
# ~18 h, and a ttl shorter than the timeout kills the pipeline mid-batch
# (caught in the 2026-08-23 preflight review — was hardcoded 28800).
RR_PIPE_TTL_S = int(os.environ.get("RR_PIPE_TTL_S") or TIMEOUT_S + 7200)
# ttl=0 = no expiry (engine source: "Tasks with ttl=0 have no timeout") —
# the matched posture uses it; only a FINITE ttl must exceed the timeout.
if RR_PIPE_TTL_S != 0 and RR_PIPE_TTL_S <= TIMEOUT_S:
    raise SystemExit(f"RR_PIPE_TTL_S ({RR_PIPE_TTL_S}) must exceed "
                     f"BENCH_TIMEOUT_S ({TIMEOUT_S}) — pipeline would expire "
                     f"before the batch timeout")
EMBED_DIM = 384
POOL_MAX = int(os.environ.get("RR_POOL_MAX", "0")) or 10 ** 9
ARM = "rocketride-docker-3.3.1-video"
VIDEO_DURS: dict = {}
# Per-node timing (ported from aws_pdf/bench/rr_driver.py, commit 7f52a17).
# Unset/empty = off, and off must stay the default: the control for a traced
# run is this same driver with RR_TRACE unset, so tracing can never silently
# become part of a baseline. Accepts the engine's literal level strings only —
# an earlier probe passed "1" (a constant's int value) and got nothing back,
# which is why flow traces were written off as unavailable for a year.
TRACE_LEVEL = (os.environ.get("RR_TRACE") or "").strip().lower() or None
if TRACE_LEVEL and TRACE_LEVEL not in ("metadata", "summary", "full"):
    raise SystemExit(f"bad RR_TRACE={TRACE_LEVEL!r}: "
                     f"expected metadata | summary | full (or unset)")


def documents_from(result):
    """Unwrap the engine's nested response shapes (same as rr_driver.py)."""
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


def verify(docs):
    if not docs:
        return False, "no documents"
    for d in docs:
        v = d.get("embedding") or []
        if len(v) != EMBED_DIM:
            return False, f"dim={len(v)}"
        if not all(x == x and abs(x) != float("inf") for x in v):
            return False, "non-finite"
    return True, ""


def base_record(video, durations):
    return {"doc": video.name, "arm": ARM, "ok": False,
            "input_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "size_bytes": video.stat().st_size,
            "duration_s": durations.get(video.name),
            "video_duration_s": VIDEO_DURS.get(video.name)}


def embedding_digest(docs):
    """sha256 over the ordered embedding sequence (haystack-suite digest) —
    lets determinism compare vectors across runs/modes without storing them."""
    import struct
    h = hashlib.sha256()
    for d in docs:
        v = d.get("embedding") or []
        h.update(struct.pack(f"<{len(v)}d", *v))
    return h.hexdigest()


def fill_from_docs(rec, docs):
    okv, why = verify(docs)
    texts = [d.get("page_content", "") for d in docs]
    rec["n_chunks"] = len(docs)
    rec["total_chars"] = sum(len(t) for t in texts)
    # Frame/detection counts for the V0 gates (frame_law, detection_ratio).
    # Concatenating chunks reassembles the full text exactly (no overlap,
    # verified), though boundary newlines are dropped by the splitter — so
    # frames are counted by their array-start patterns, which concatenation
    # restores even when a split fell mid-line or between '[' and '{'.
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
    return okv, why


def records_from_batch(corpus, out, t0_ns, event_times, durations):
    """Per-doc records from ONE batched send_files() response — attribution by
    filepath basename, derived timings marked as derived (see rr_driver.py)."""
    items = out if isinstance(out, list) else [out]
    by_name = {}
    for it in items:
        if isinstance(it, dict):
            fp = it.get("filepath")
            if isinstance(fp, str) and fp:
                by_name.setdefault(Path(fp).name, it)

    recs = []
    for video in corpus:
        it = by_name.get(video.name)
        rec = base_record(video, durations)
        rec["submit_ns"] = t0_ns
        rec["timing_source"] = "batch_upload_time (derived, not measured)"
        if it is None:
            rec["completion_ns"] = t0_ns
            rec["identity_ok"] = False
            rec["reason"] = "no_response_for_file"
            recs.append(rec)
            continue
        ut = it.get("upload_time")
        ev = event_times.get(video.name)
        if ev is not None:
            rec["completion_ns"] = ev
            rec["timing_source"] = "client-observed upload event"
        elif isinstance(ut, (int, float)):
            rec["completion_ns"] = t0_ns + int(float(ut) * 1e9)
        else:
            rec["completion_ns"] = t0_ns
        rec["upload_time_s"] = ut
        rec["identity_ok"] = True
        docs = documents_from(it)
        okv, why = fill_from_docs(rec, docs)
        rec["ok"] = bool(okv)
        if rec["ok"]:
            rec["reason"] = "completed"
        elif not docs:
            rec["reason"] = "no_documents"
            rec["error_raw"] = json.dumps(it, default=str)[:400]
        else:
            rec["reason"] = "completion_proof_missing"
            rec["error"] = why
        recs.append(rec)
    return recs


async def main():
    from rocketride import RocketRideClient

    corpus_dir, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "blast"
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
    # Footage denominator (2026-08-23): the PROBED video-stream duration is
    # authoritative for this detect-only benchmark — frames come from the
    # video clock, not the container/source metadata (the AMI A/V lesson).
    # Source metadata kept separately for transparency; probed falls back to
    # source only where a probe is absent (old AMI manifests).
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

    pipe = json.loads(PIPE_SRC.read_text())
    pipe["project_id"] = str(uuid.uuid4())
    pipe_path = out / "pipeline.pipe"
    pipe_path.write_text(json.dumps(pipe))

    print(f"[rrv] corpus={len(corpus)} docs, "
          f"{measured_video_s/3600:.2f} h of footage (video-stream probed), "
          f"mode={mode}, warm={warm_docs}, timeout={TIMEOUT_S}s, "
          f"pipe_ttl={RR_PIPE_TTL_S}s", flush=True)

    event_times: dict = {}
    event_actions: collections.Counter = collections.Counter()
    # Live per-video progress, flushed line-by-line as terminal events arrive,
    # so a watcher (and the S3 sync loop) sees completions DURING a blast
    # instead of one blob at the end. Append-only; the post-run per_doc.jsonl
    # stays the authoritative record.
    progress_fh = open(out / "progress.jsonl", "a", buffering=1)
    run_t0 = time.perf_counter_ns()

    # PER-NODE TIMING (RR_TRACE).
    #
    # The engine emits apaevt_flow with op=begin|enter|leave|end and a
    # `component` field — real per-node boundaries — but ONLY when the task was
    # started with a pipelineTraceLevel, and NO event carries a timestamp. So
    # the clock is supplied here: stamp arrival, pair enter/leave, diff.
    # Delivery latency is common-mode across one socket and cancels in the
    # diff. Pair by (pipe id, component) as the docs require — never by stack
    # position, since components nest and repeat.
    #
    # On THIS pipe the nesting is the whole story: the engine drives
    # frame_grabber_1 ⊃ detect_1 ⊃ preprocessor_1 ⊃ embedding_1 ⊃ response_1
    # synchronously, so inclusive totals always indict frame_grabber_1.
    # flow_selftime.summarize_flow subtracts the children — see that module.
    flow_raw: list = []
    flow_counts: collections.Counter = collections.Counter()

    async def on_event(ev):
        try:
            name_ev = ev.get("event")
            if name_ev == "apaevt_status_upload":
                b = ev.get("body") or {}
                act = b.get("action")
                event_actions[act] += 1
                if act in ("complete", "error"):
                    fp = b.get("filepath")
                    if fp:
                        name = Path(fp).name
                        event_times.setdefault(name, time.perf_counter_ns())
                        done = len(event_times)
                        t_rel = (time.perf_counter_ns() - run_t0) / 1e9
                        progress_fh.write(json.dumps(
                            {"doc": name, "action": act,
                             "t_rel_s": round(t_rel, 1), "n_done": done}) + "\n")
                        print(f"[rrv] progress: {name} {act} "
                              f"({done} terminal events, t+{t_rel:.0f}s)",
                              flush=True)
                return
            if TRACE_LEVEL and name_ev == "apaevt_flow":
                # Append-only, O(1), no parsing: this handler is awaited inline
                # on the SDK receive path and blocking it closes the websocket.
                # Everything is decoded after the run.
                b = ev.get("body") or {}
                flow_raw.append((time.perf_counter_ns(), b.get("id"),
                                 b.get("op"), b.get("component")))
                flow_counts[b.get("op")] += 1
        except Exception:
            pass

    # RR_THREADS unset -> the engine's own default pool (what an untuned
    # deployment gets; every video run before 2026-08-20 ran this way).
    # Set -> that pool size is requested via use(threads=N). Same caveat as
    # the PDF bench: threads REQUESTED is not threads activated — the real
    # pool is not observable from the client; effective cores come from the
    # cgroup sampler.
    rr_threads = os.environ.get("RR_THREADS")
    threads = int(rr_threads) if rr_threads else None

    client = RocketRideClient(uri=URI, auth=APIKEY, on_event=on_event)
    await client.connect()
    use_kwargs = dict(filepath=str(pipe_path), use_existing=True,
                      ttl=RR_PIPE_TTL_S)
    if TRACE_LEVEL:
        # The literal level string — 'metadata' | 'summary' | 'full'. 'full'
        # serializes every lane write and would itself burn the CPU we are
        # trying to attribute; 'summary' is the default to reach for.
        use_kwargs["pipelineTraceLevel"] = TRACE_LEVEL
    if threads:
        use_kwargs["threads"] = threads
    used = await client.use(**use_kwargs)
    token = used["token"]
    print(f"[rrv] pipeline up, token={token}, threads_requested="
          f"{threads if threads else 'NONE (engine default)'}", flush=True)

    # NOTE: this call is INERT and kept only because every prior run carried
    # it. `types` wants EVENT_TYPE enum names (FLOW, TASK, ...), not wire event
    # names, so the engine logs "Unknown event type 'apaevt_status_upload'
    # ignored". It never mattered: upload progress is delivered unconditionally
    # to the uploading connection. Left untouched so this change cannot perturb
    # the established measurement path; the real subscription is add_monitor().
    try:
        await client.set_events(token, ["apaevt_status_upload"])
    except Exception as exc:
        print(f"[rrv] set_events unavailable ({type(exc).__name__}); per-doc "
              f"latency falls back to derived", flush=True)

    if TRACE_LEVEL:
        # EVENT_TYPE enum names, not wire names — this is the subscription that
        # actually delivers apaevt_flow. Failing loudly: a silent miss here
        # produces an empty node table that looks like "the engine has no
        # per-node cost", which is the wrong conclusion to publish.
        try:
            await client.add_monitor({"token": token}, ["flow", "task"])
            print(f"[rrv] tracing ON: pipelineTraceLevel={TRACE_LEVEL!r}, "
                  f"subscribed flow+task", flush=True)
        except Exception as exc:
            print(f"[rrv] FATAL: add_monitor failed ({type(exc).__name__}: "
                  f"{exc})", flush=True)
            raise

    # Warm-start with docs DISJOINT from the measured set (list tail).
    warm_set = all_videos[n:n + warm_docs]
    warm_s = None
    if warm_docs and len(warm_set) < warm_docs:
        print(f"[rrv] WARNING: only {len(warm_set)} warm docs available", flush=True)
    if warm_set:
        tw = time.perf_counter_ns()
        await asyncio.wait_for(
            client.send_files([(str(p), {"doc_id": f"warm-{p.stem}"})
                               for p in warm_set], token),
            timeout=TIMEOUT_S)
        warm_s = (time.perf_counter_ns() - tw) / 1e9
        event_times.clear(); event_actions.clear()
        print(f"[rrv] warm-start: {len(warm_set)} docs in {warm_s:.1f}s "
              f"(excluded from measurement)", flush=True)

    mono_offset_ns = time.time_ns() - time.perf_counter_ns()
    t0 = time.perf_counter_ns()

    if mode == "blast":
        payload = [(str(v), {"doc_id": v.stem}) for v in corpus]
        batch_result = await asyncio.wait_for(client.send_files(payload, token),
                                              timeout=TIMEOUT_S)
        span = (time.perf_counter_ns() - t0) / 1e9
        span_ns = time.perf_counter_ns() - t0
        usable = dict(event_times)
        if usable:
            last = max(usable.values()) - t0
            frac = last / span_ns if span_ns else 0
            if frac < 0.5:
                print(f"[rrv] upload events done at {frac:.0%} of span -> they "
                      f"signal UPLOAD, not processing; falling back to derived",
                      flush=True)
                usable = {}
            else:
                print(f"[rrv] client-observed completions for {len(usable)}/"
                      f"{len(corpus)} (last at {frac:.0%} of span)", flush=True)
        records = records_from_batch(corpus, batch_result, t0, usable, durations)
    else:
        if mode == "seq":
            offered = 1
        elif mode.startswith("c") and mode[1:].isdigit():
            offered = int(mode[1:])
        else:
            raise SystemExit(f"bad mode {mode!r}: expected blast | seq | c<N>")
        sem = asyncio.Semaphore(offered)
        done = [0]

        async def one(video):
            async with sem:
                rec = base_record(video, durations)
                rec["submit_ns"] = time.perf_counter_ns()
                try:
                    up = await asyncio.wait_for(
                        client.send_files([(str(video), {"doc_id": video.stem})],
                                          token),
                        timeout=TIMEOUT_S)
                    rec["completion_ns"] = time.perf_counter_ns()
                    items = [u for u in (up if isinstance(up, list) else [])
                             if isinstance(u, dict)
                             and u.get("filepath") == str(video)]
                    rec["identity_ok"] = bool(items)
                    docs = documents_from(items if items else up)
                    okv, why = fill_from_docs(rec, docs)
                    rec["ok"] = bool(okv and items)
                    rec["reason"] = ("completed" if rec["ok"] else
                                     "no_documents" if not docs else
                                     "completion_proof_missing")
                except asyncio.TimeoutError:
                    rec["completion_ns"] = time.perf_counter_ns()
                    rec["reason"] = "timeout"
                except Exception as exc:
                    rec["completion_ns"] = time.perf_counter_ns()
                    rec["reason"] = "transport_error"
                    rec["error"] = f"{type(exc).__name__}: {exc}"[:250]
                done[0] += 1
                print(f"[rrv] {done[0]}/{len(corpus)} {video.name} "
                      f"{rec['reason']}", flush=True)
                return rec

        records = list(await asyncio.gather(*[one(v) for v in corpus]))
        span = (time.perf_counter_ns() - t0) / 1e9

    # ---- per-node timing, derived after the run (never in the handler) ----
    #
    # The self-time arithmetic lives in flow_selftime.py so the same code
    # produces the in-run table and the offline per-document/LangGraph
    # comparison — two implementations of "subtract the children" is how the
    # two numbers end up disagreeing. Imported lazily and only when tracing is
    # on, so an untraced control run is byte-identical to before this change
    # (no import, no side effects, no dependency on the module being mounted).
    node_summary = None
    if TRACE_LEVEL:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from flow_selftime import summarize_flow
        node_summary = summarize_flow(flow_raw, t0, span)
        (out / "flow_events.jsonl").write_text("".join(
            json.dumps({"ts_ns": ts, "pipe": pid, "op": op, "component": comp})
            + "\n" for ts, pid, op, comp in flow_raw))
        (out / "node_timings.json").write_text(json.dumps(node_summary, indent=1))
        print(f"[rrv] flow events: {dict(flow_counts)}", flush=True)
        if node_summary.get("nodes"):
            print(f"[rrv] {'node':<24}{'calls':>8}{'self s':>10}{'% self':>8}"
                  f"{'p50 ms':>10}{'p95 ms':>10}{'max ms':>11}{'incl s':>11}",
                  flush=True)
            for nm, v in node_summary["nodes"].items():
                print(f"[rrv] {nm:<24}{v['calls']:>8}{v['total_s']:>10.1f}"
                      f"{(v['pct_of_node_time'] or 0):>8.1f}{v['p50_ms']:>10.1f}"
                      f"{v['p95_ms']:>10.1f}{v['max_ms']:>11.1f}"
                      f"{v['inclusive_total_s']:>11.1f}", flush=True)
            print(f"[rrv] nested={node_summary['nested']} "
                  f"max_depth={node_summary['max_nesting_depth']} "
                  f"unmatched_leave={node_summary['unmatched_leave']} "
                  f"unclosed_enter={node_summary['unclosed_enter']}"
                  + ("  (self s is EXCLUSIVE of nested children; 'incl s' for "
                     "the outermost node is the whole pipeline)"
                     if node_summary["nested"] else ""), flush=True)
            print("[rrv] 'calls' are LANE CALLS, not documents — a node gets "
                  "several enter/leave pairs per video", flush=True)
        else:
            print("[rrv] WARNING: tracing was ON but no enter/leave pairs were "
                  "captured — flow traces did not arrive", flush=True)

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
            # legacy alias (pre-2026-08-23 readers) — now carries the probed
            # denominator, same as measured_video_s
            "measured_audio_s": measured_video_s,
            "realtime_factor": round(measured_video_s / span, 2) if span else None,
            "total_chunks": chunks,
            "timeout_s": TIMEOUT_S,
            "offered_concurrency": (len(corpus) if mode == "blast" else
                                    1 if mode == "seq" else int(mode[1:])),
            "pipe_sha256": hashlib.sha256(PIPE_SRC.read_bytes()).hexdigest(),
            "provenance": {"pipeline_kind": "detect", "interval_s": 15,
                           "detect_model": "rfdetr", "threshold": 0.3,
                           "embed_model": "multi-qa-MiniLM-L6-cos-v1",
                           "split": {"chunk_size": 4000, "overlap": 0},
                           "expect_dim": 384,
                           "bench_timeout_s": TIMEOUT_S,
                           "rr_pipe_ttl_s": RR_PIPE_TTL_S,
                           "duration_authority": "ffmpeg-probed video stream"},
            "threads_requested": threads,
            "warm_docs": len(warm_set), "warm_s": warm_s,
            "event_actions_seen": dict(event_actions),
            "mono_offset_ns": mono_offset_ns,
            # Measured-interval markers (epoch ns): warm-up, model load and
            # driver startup are BEFORE start — the report windows the cgroup
            # sampler to [start, end] so CPU excludes them.
            "measurement_start_epoch_ns": mono_offset_ns + t0,
            "measurement_end_epoch_ns": mono_offset_ns + t0 + int(span * 1e9),
            "envelope": "NONE — sizing run: no cpuset, engine threads unpinned",
            "trace_level": TRACE_LEVEL,
            "flow_ops_seen": dict(flow_counts) if TRACE_LEVEL else None,
            "node_timings": node_summary,
        }) + "\n")

    print(f"[rrv] DONE mode={mode}: {ok_n}/{len(records)} ok, "
          f"{chunks} chunks, span={span:.1f}s "
          f"({measured_video_s/3600:.2f} h footage -> "
          f"{measured_video_s/span:.1f}x realtime)", flush=True)

    try:
        await client.terminate(token)
    except Exception as exc:
        print(f"[rrv] terminate failed: {type(exc).__name__}: {exc}", flush=True)
    try:
        await client.disconnect()
    except Exception:
        pass
    if ok_n < len(records):
        raise SystemExit(f"RUN INCOMPLETE: {len(records) - ok_n} docs not ok")


if __name__ == "__main__":
    asyncio.run(main())
