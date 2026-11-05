"""RocketRide driver — runs in the bench CLIENT container.

Runs in the bench CLIENT container, never inside the engine's container: the
client's own CPU and RSS must not land in the server's cgroup, and both arms
must receive their bytes over an equivalent network hop.

Writes per_doc.jsonl in the schema metrics/ consumes, satisfying the RR record
contract from metrics.m0_correctness.REQUIRED_TRUE["rr"] (identity_ok). Vector
finiteness is folded into vector_dim exactly as gate50/rr_native_gate.py does:
a non-finite vector can never reach a dim of 384.

Modes: seq (closed-loop, true service latency); blast (ONE batched
send_files -- the engine holds the backlog and schedules it); c<N>
(closed-loop, N single-file send_files in flight); b<B> (the corpus is
submitted as CONSECUTIVE send_files batches of B documents on ONE connection,
batch k+1 submitted when batch k returns -- in-flight work bounded by B and
by the engine's own per-connection admission cap).

  python3 rr_smoke_driver.py <corpus_dir> <out_dir> <n_docs>
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

# Reached over the network from the bench client container, NOT from inside
# the engine's own container -- see bench.Dockerfile for why that matters.
URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_pdf.pipe"))
WARMUP_DOC = Path(os.environ.get("BENCH_WARMUP_DOC", "/pipe/sample.pdf"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "3600"))
# ONE deadline for both arms and both modes. Previously LangGraph had 300s
# per request while a RocketRide batch had 3600s, so a healthy LangGraph doc
# late in a 200-doc backlog could be killed by queue wait while the equivalent
# RocketRide batch ran on for an hour. Not a matched condition.
BATCH_TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "3600"))
EMBED_DIM = 384
# Client sockets, not offered concurrency. Default 0 = no client-side cap
# (one socket per in-flight doc), so the engine's scheduler is the only
# limiter. Set RR_POOL_MAX to reimpose one.
POOL_MAX = int(os.environ.get("RR_POOL_MAX", "0")) or 10 ** 9
# Per-node timing. Unset/empty = off, and off must stay the default: the
# control for a traced run is this same driver with RR_TRACE unset.
# Accepts the engine's literal level strings only.
TRACE_LEVEL = (os.environ.get("RR_TRACE") or "").strip().lower() or None
if TRACE_LEVEL and TRACE_LEVEL not in ("metadata", "summary", "full"):
    raise SystemExit(f"bad RR_TRACE={TRACE_LEVEL!r}: "
                     f"expected metadata | summary | full (or unset)")


def documents_from(result):
    """Unwrap the engine's nested response shapes. Same logic as
    gate50/rr_native_gate.py -- do not simplify without re-checking a raw
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


def records_from_batch(corpus, out, t0_ns, mono_offset_ns, event_times=None):
    """Per-doc records from ONE batched send_files() response.

    Timing note, stated in every record: a batch has one submit instant and
    one return instant, so there are no measured per-document timestamps.
    submit_ns is the batch start (true -- every file WAS submitted then) and
    completion_ns is derived from the engine's own per-file `upload_time`.
    `timing_source` marks this so a derived value is never mistaken for a
    measured one. The batch span itself is measured exactly, so M1 is exact;
    only per-doc M2 is an approximation.

    Attribution is by filepath basename, not list position: position-based
    zip silently mis-credits work if the engine reorders. A file with no
    matching response is recorded as a failure, never dropped.
    """
    event_times = event_times or {}
    items = out if isinstance(out, list) else [out]
    by_name = {}
    for it in items:
        if isinstance(it, dict):
            fp = it.get("filepath")
            if isinstance(fp, str) and fp:
                by_name.setdefault(Path(fp).name, it)

    recs = []
    for pdf in corpus:
        it = by_name.get(pdf.name)
        rec = {"doc": pdf.name, "arm": "rocketride-docker", "ok": False,
               "input_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
               "size_bytes": pdf.stat().st_size,
               "submit_ns": t0_ns,
               "timing_source": "batch_upload_time (derived, not measured)"}
        if it is None:
            rec["completion_ns"] = t0_ns
            rec["identity_ok"] = False
            rec["reason"] = "no_response_for_file"
            recs.append(rec)
            continue
        ut = it.get("upload_time")
        ev = event_times.get(pdf.name)
        if ev is not None:
            # Client-observed: the clock was stamped when this file's terminal
            # upload event arrived. Real latency, not reconstructed.
            rec["completion_ns"] = ev
            rec["timing_source"] = "client-observed upload event"
        elif isinstance(ut, (int, float)):
            rec["completion_ns"] = t0_ns + int(float(ut) * 1e9)
        else:
            rec["completion_ns"] = t0_ns
        rec["upload_time_s"] = ut
        rec["identity_ok"] = True          # matched by filepath basename
        docs = documents_from(it)
        okv, why = verify(docs)
        texts = [d.get("page_content", "") for d in docs]
        rec["n_chunks"] = len(docs)
        rec["total_chars"] = sum(len(t) for t in texts)
        rec["chunk_sha256"] = [hashlib.sha256(t.encode("utf-8")).hexdigest()
                               for t in texts]
        rec["vector_dim"] = EMBED_DIM if okv else None
        norms = [sum(x * x for x in (d.get("embedding") or [])) ** 0.5
                 for d in docs]
        rec["l2_norms_minmax"] = ([round(min(norms), 6), round(max(norms), 6)]
                                  if norms else None)
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


def summarize_flow(flow_raw, t0_ns, span_s):
    """Per-node wall time from paired apaevt_flow enter/leave arrivals.

    The engine names the boundaries but stamps no clock, so these are CLIENT
    arrival times. Websocket delivery latency is common-mode across one socket
    and cancels in a leave-minus-enter difference; what it cannot cancel is
    jitter, so treat small differences between nodes as noise and read the
    shape, not the third digit.

    Pairing is by (pipe id, component) with a stack, which the observability
    doc requires -- components can repeat within a pipe and stack position is
    not identity. Durations are therefore INCLUSIVE of anything nested inside;
    `nested` reports whether that distinction bites on this pipeline (a linear
    chain should never nest, and if it does the totals are not additive).

    Events that arrive before the measured span begins (warm-up) are dropped,
    so this covers the same window as the throughput number.
    """
    opens: dict = {}
    intervals: dict = {}
    unmatched_leave = 0
    depth_by_pipe: dict = {}
    max_depth = 0
    for ts, pipe, op, comp in flow_raw:
        if op == "enter":
            d = depth_by_pipe.get(pipe, 0) + 1
            depth_by_pipe[pipe] = d
            max_depth = max(max_depth, d)
            opens.setdefault((pipe, comp), []).append(ts)
        elif op == "leave":
            depth_by_pipe[pipe] = max(0, depth_by_pipe.get(pipe, 0) - 1)
            stack = opens.get((pipe, comp))
            if not stack:
                unmatched_leave += 1
                continue
            start = stack.pop()
            if start >= t0_ns:            # warm-up excluded
                intervals.setdefault(comp or "<unnamed>", []).append(ts - start)
    unclosed = sum(len(v) for v in opens.values())

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(p * len(xs)))] / 1e6

    grand = sum(sum(v) for v in intervals.values()) / 1e9
    nodes = {}
    for comp, durs in sorted(intervals.items(),
                             key=lambda kv: -sum(kv[1])):
        durs.sort()
        total = sum(durs) / 1e9
        nodes[comp] = {
            "calls": len(durs),
            "total_s": round(total, 3),
            # Share of summed node time -- THE figure to quote. With C
            # documents in flight the node totals sum to roughly C x span, so
            # pct_of_span below can legitimately exceed 100 and must never be
            # read as "fraction of the run".
            "pct_of_node_time": round(100 * total / grand, 2) if grand else None,
            "pct_of_span": round(100 * total / span_s, 2) if span_s else None,
            "p50_ms": round(pct(durs, 0.50), 3),
            "p95_ms": round(pct(durs, 0.95), 3),
            "max_ms": round(durs[-1] / 1e6, 3),
        }
    return {
        "basis": "client arrival timestamps on paired apaevt_flow enter/leave; "
                 "durations are INCLUSIVE of nested components",
        "span_s": round(span_s, 3),
        "nodes": nodes,
        "nested": max_depth > 1,
        "max_nesting_depth": max_depth,
        "unmatched_leave": unmatched_leave,
        "unclosed_enter": unclosed,
    }


async def main():
    from rocketride import RocketRideClient
    from profile_capture import ProfileCapture

    corpus_dir, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    all_pdfs = sorted(corpus_dir.glob("*.pdf"))
    corpus = all_pdfs[:n]
    if not corpus:
        raise SystemExit(f"no PDFs in {corpus_dir}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({"docs": [p.name for p in corpus], "n": len(corpus)}))

    pipe = json.loads(PIPE_SRC.read_text())
    pipe["project_id"] = str(uuid.uuid4())
    pipe_path = out / "pipeline.pipe"
    pipe_path.write_text(json.dumps(pipe))

    # RR_THREADS unset -> the engine's own default pool (what an untuned
    # deployment gets). Set -> that pool size is requested explicitly.
    #
    # Two prior runs measured ~5.8-5.9 effective cores on a 32-core box with
    # 372-399 threads alive and client concurrency of 8 then 11 -- parallelism
    # did not move, so the limiter is the engine's pool, not the client. The
    # comparable Haystack-suite run passes threads=<host cores> and reaches
    # 24.28 cores at 76% CPU. This makes that the variable under test.
    rr_threads = os.environ.get("RR_THREADS")
    threads = int(rr_threads) if rr_threads else None

    # CLIENT-OBSERVED per-file completion times.
    #
    # A batched send_files() returns one result for the whole batch, so blast
    # had no measured per-document timestamps and latency was withheld. The
    # SDK does emit apaevt_status_upload events per file; stamping the clock
    # when each file's terminal event arrives gives a real client-observed
    # completion, not a value derived from the engine's own upload_time.
    #
    # The handler must not perturb what it measures: it filters on the first
    # dict lookup, does O(1) work, and swallows everything -- instrumentation
    # that can throw into the SDK's event loop would change the run.
    event_times: dict = {}
    event_actions: collections.Counter = collections.Counter()

    # PER-NODE TIMING (RR_TRACE).
    #
    # The engine emits apaevt_flow with op=begin|enter|leave|end and a
    # `component` field -- real per-node boundaries -- but ONLY when the task
    # was started with a pipelineTraceLevel, and NO event carries a timestamp.
    # So the clock is supplied here: stamp arrival, pair enter/leave, diff.
    # Delivery latency is common-mode across one socket and cancels in the
    # diff. Pair by (pipe id, component) as the docs require -- never by stack
    # position, since components can nest and repeat.
    #
    # Off by default: the control run for this measurement is the SAME code
    # with RR_TRACE unset, so tracing can never silently be part of a baseline.
    flow_raw: list = []
    flow_counts: collections.Counter = collections.Counter()

    async def on_event(ev):
        try:
            name = ev.get("event")
            if name == "apaevt_status_upload":
                b = ev.get("body") or {}
                act = b.get("action")
                event_actions[act] += 1
                if act in ("complete", "error"):
                    fp = b.get("filepath")
                    if fp:
                        event_times.setdefault(Path(fp).name, time.perf_counter_ns())
                return
            if TRACE_LEVEL and name == "apaevt_flow":
                # Append-only, O(1), no parsing: this handler is awaited inline
                # on the SDK receive path and blocking it for ~60 s closes the
                # websocket. Everything is decoded after the run.
                b = ev.get("body") or {}
                flow_raw.append((time.perf_counter_ns(), b.get("id"),
                                 b.get("op"), b.get("component")))
                flow_counts[b.get("op")] += 1
        except Exception:
            pass

    client = RocketRideClient(uri=URI, auth=APIKEY, on_event=on_event)
    await client.connect()
    use_kwargs = dict(filepath=str(pipe_path), use_existing=True, ttl=7200)
    if TRACE_LEVEL:
        # The literal level string -- 'metadata' | 'summary' | 'full'. An
        # earlier probe in this repo passed "1" (a constant's int value) and
        # got nothing back, which is why flow traces were written off as
        # unavailable. 'full' serializes every lane write and would itself
        # burn the CPU we are trying to attribute; 'summary' is the default.
        use_kwargs["pipelineTraceLevel"] = TRACE_LEVEL
    if threads:
        use_kwargs["threads"] = threads
    if threads:
        # Prove the value reached use(). "threads requested" is a claim in
        # provenance; this makes it a verified one. It is still NOT evidence
        # that the engine activated that many workers.
        assert use_kwargs.get("threads") == threads, (
            f"threads={threads} did not reach use(): {use_kwargs}")
    used = await client.use(**use_kwargs)
    token = used["token"]
    print(f"[rr] pipeline up, token={token}, "
          f"threads_requested={threads if threads else 'NONE (engine default)'}",
          flush=True)

    # NOTE: this call is INERT and kept only because every prior run carried
    # it. `types` wants EVENT_TYPE enum names (FLOW, SUMMARY, TASK, ...), not
    # wire event names, so the engine logs "Unknown event type
    # 'apaevt_status_upload' ignored" -- visible in every engine log we have.
    # It never mattered: upload progress is delivered unconditionally to the
    # uploading connection, which is why per-doc completion times worked all
    # along. Left untouched so this change cannot perturb the established
    # measurement path; the correct subscription is add_monitor() below.
    try:
        await client.set_events(token, ["apaevt_status_upload"])
    except Exception as exc:
        print(f"[rr] set_events unavailable ({type(exc).__name__}); per-doc "
              f"latency will fall back to derived", flush=True)

    if TRACE_LEVEL:
        try:
            await client.add_monitor({"token": token}, ["flow", "task"])
            print(f"[rr] tracing ON: pipelineTraceLevel={TRACE_LEVEL!r}, "
                  f"subscribed flow+task", flush=True)
        except Exception as exc:
            print(f"[rr] FATAL: add_monitor failed ({type(exc).__name__}: {exc})",
                  flush=True)
            raise

    mode = sys.argv[4] if len(sys.argv) > 4 else "seq"
    warm_docs = int(sys.argv[5]) if len(sys.argv) > 5 else 0

    # Warm-start: WARM_DOCS real documents pushed through before the measured
    # span, timed separately and excluded. One tiny fixture is not enough --
    # a cold engine pays model load, JIT and cache fill, and with an unknown
    # pool size a single document leaves most workers cold at t=0.
    # Warm-up documents are DISJOINT from measured ones (see lg_driver).
    warm_set = all_pdfs[n:n + warm_docs]
    warm_disjoint = len(warm_set) == warm_docs
    if warm_docs > 0 and not warm_disjoint:
        warm_set = corpus[:warm_docs]
        print(f"[rr] WARNING: only {len(all_pdfs)} PDFs for n={n}+warm="
              f"{warm_docs}; warm-up REUSES measured docs", flush=True)
    warm_s = None
    if warm_docs > 0:
        tw = time.perf_counter_ns()
        await asyncio.wait_for(
            client.send_files([(str(p), {"doc_id": f"warm-{p.stem}"})
                               for p in warm_set], token),
            timeout=TIMEOUT_S)
        warm_s = (time.perf_counter_ns() - tw) / 1e9
        event_times.clear(); event_actions.clear()   # warm-up is not measured
        print(f"[rr] warm-start: {len(warm_set)} docs in {warm_s:.1f}s "
              f"(excluded from measurement)", flush=True)
    else:
        up = await asyncio.wait_for(
            client.send_files([(str(WARMUP_DOC), {"doc_id": "warm"})], token),
            timeout=120)
        ok, why = verify(documents_from(up))
        if not ok:
            raise SystemExit(f"WARMUP FAILED: {why}")
        print("[rr] warmup ok (1 fixture doc)", flush=True)

    batch_size = None
    if mode == "seq":
        offered = 1
    elif mode == "blast":
        offered = len(corpus)
    elif mode.startswith("c") and mode[1:].isdigit():
        offered = int(mode[1:])
    elif mode.startswith("b") and mode[1:].isdigit() and int(mode[1:]) > 0:
        # b<B>: consecutive send_files() batches of B documents on ONE
        # connection, each submitted when the previous one has returned. The
        # engine never sees more than B documents of this run at once, so
        # "offered" is the batch size (capped by the corpus). The engine's own
        # per-connection admission (threads=64 by default) is the other bound.
        batch_size = int(mode[1:])
        offered = min(batch_size, len(corpus))
    else:
        raise SystemExit(f"bad mode {mode!r}: expected seq | blast | c<N> | b<B>")

    # Sampler ts is wall-clock epoch, per-doc ts is monotonic; this offset is
    # what lets m7_resources.window() slice the sampler to EXACTLY the
    # throughput window.
    mono_offset_ns = time.time_ns() - time.perf_counter_ns()

    # Client socket pool. Each send_files() call appears to occupy its
    # connection for the round trip, so the pool -- not `offered` -- is the
    # real concurrency ceiling. The 200-doc run at pool=8 measured a 290.7 s
    # span, and 200/8 x 11.2 s (its own p50) = 280 s: the client was the
    # limiter, not the engine. Default is now NO client-side cap, so the
    # engine's own scheduling is what is under test.
    # Blast issues ONE send_files() on ONE client, so a pool is pointless --
    # and building one triggered 189 refused connections (the engine accepts
    # ~11 concurrent). Only the per-doc modes need multiple sockets.
    # b<B> likewise: one connection carries every batch, by definition.
    pool_size = 1 if (mode == "blast" or batch_size) else min(offered, POOL_MAX)
    pool = [client]

    async def add_client():
        c = RocketRideClient(uri=URI, auth=APIKEY)
        await c.connect()
        # use_existing + the same project_id: all clients attach to the ONE
        # pipeline instance rather than each spawning a backend.
        await c.use(filepath=str(pipe_path), use_existing=True, ttl=7200)
        return c

    if pool_size > 1:
        t_pool = time.perf_counter_ns()
        # Concurrently: 200 sequential connect+use round trips would take
        # longer than the measured span itself.
        extra = await asyncio.gather(*[add_client() for _ in range(pool_size - 1)],
                                     return_exceptions=True)
        failed = [e for e in extra if isinstance(e, Exception)]
        pool.extend([c for c in extra if not isinstance(c, Exception)])
        print(f"[rr] pool: {len(pool)} clients in "
              f"{(time.perf_counter_ns() - t_pool) / 1e9:.1f}s"
              + (f" ({len(failed)} failed: {type(failed[0]).__name__})" if failed else ""),
              flush=True)
    print(f"[rr] mode={mode} offered={offered} pool={len(pool)} "
          f"(pool_cap={POOL_MAX})", flush=True)

    sem = asyncio.Semaphore(offered)
    done = [0]

    async def one(i, pdf):
        async with sem:
            cl = pool[i % len(pool)]
            rec = {"doc": pdf.name, "arm": "rocketride-docker", "ok": False,
                   "input_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                   "size_bytes": pdf.stat().st_size,
                   "submit_ns": time.perf_counter_ns()}
            try:
                up = await asyncio.wait_for(
                    cl.send_files([(str(pdf), {"doc_id": pdf.stem})], token),
                    timeout=TIMEOUT_S)
                rec["completion_ns"] = time.perf_counter_ns()
                items = [u for u in (up if isinstance(up, list) else [])
                         if isinstance(u, dict) and u.get("filepath") == str(pdf)]
                rec["identity_ok"] = bool(items)
                docs = documents_from(items if items else up)
                okv, why = verify(docs)
                texts = [d.get("page_content", "") for d in docs]
                rec["n_chunks"] = len(docs)
                rec["total_chars"] = sum(len(t) for t in texts)
                rec["chunk_sha256"] = [
                    hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
                rec["vector_dim"] = EMBED_DIM if okv else None
                norms = [sum(x * x for x in (d.get("embedding") or [])) ** 0.5
                         for d in docs]
                rec["l2_norms_minmax"] = ([round(min(norms), 6), round(max(norms), 6)]
                                          if norms else None)
                rec["ok"] = bool(okv and items)
                if rec["ok"]:
                    rec["reason"] = "completed"
                elif not docs:
                    rec["reason"] = "no_documents"
                    rec["error_raw"] = json.dumps(up, default=str)[:400]
                else:
                    rec["reason"] = "completion_proof_missing"
                    rec["error"] = why or "identity"
            except asyncio.TimeoutError:
                rec["completion_ns"] = time.perf_counter_ns()
                rec["reason"] = "timeout"
            except Exception as exc:
                rec["completion_ns"] = time.perf_counter_ns()
                rec["reason"] = "transport_error"
                rec["error"] = f"{type(exc).__name__}: {exc}"[:250]
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(corpus):
                print(f"[rr] {done[0]}/{len(corpus)}", flush=True)
            return rec

    client_observed = None      # how many per-file completions were CLIENT-observed
    n_batches = None
    batch_spans = []
    profile = ProfileCapture.from_env(client, token, out)
    async with profile:
        t0 = time.perf_counter_ns()
        if mode == "blast":
            # ONE call carrying the whole backlog. The engine holds the batch and
            # its own pool schedules it -- this is how the SDK is built to work
            # (send_files gathers one coroutine per file, "let server handle
            # queuing"). Submitting 200 single-file calls instead pins concurrency
            # to the connection count, which the engine caps around 11: measured
            # 5.8-6.0 effective cores across pool=8, pool=11 and threads=32, while
            # the comparable Haystack-suite run reaches 24.28 by batching.
            payload = [(str(p), {"doc_id": p.stem}) for p in corpus]
            # NOT `out`: that name is the output DIRECTORY (a Path) and rebinding
            # it turns the later `out / "per_doc.jsonl"` into list / str.
            batch_result = await asyncio.wait_for(client.send_files(payload, token),
                                                  timeout=BATCH_TIMEOUT_S)
            span = (time.perf_counter_ns() - t0) / 1e9
            # If every terminal event landed long before the batch returned, the
            # events signal UPLOAD completion, not processing completion. Using
            # them as latency would understate it badly, so they are discarded and
            # the run falls back to derived timings (which the report withholds).
            span_ns = time.perf_counter_ns() - t0
            usable = dict(event_times)
            if usable:
                last = max(usable.values()) - t0
                frac = last / span_ns if span_ns else 0
                if frac < 0.5:
                    print(f"[rr] upload events completed at {frac:.0%} of the batch "
                          f"span -> they signal UPLOAD, not processing; DISCARDING "
                          f"and falling back to derived timings", flush=True)
                    usable = {}
                else:
                    print(f"[rr] client-observed completions for {len(usable)}/"
                          f"{len(corpus)} files (last at {frac:.0%} of span)",
                          flush=True)
            records = records_from_batch(corpus, batch_result, t0, mono_offset_ns,
                                         usable)
            client_observed = len(usable)
            returned = len(batch_result) if isinstance(batch_result, list) else 1
            matched = sum(1 for r in records if r.get("reason") != "no_response_for_file")
            print(f"[rr] batch returned {returned} items for {len(corpus)} files; "
                  f"{matched} attributed by filepath", flush=True)
        elif batch_size:
            # b<B>: the corpus in CONSECUTIVE batches of B, each ONE send_files()
            # call on ONE connection, submitted back-to-back. The measured span
            # runs from the first batch's submit to the last batch's return, so
            # every inter-batch turnaround is inside it -- that turnaround is a
            # real cost of batching, not overhead to be excluded. Each batch is
            # attributed exactly like blast (by filepath, client-observed upload
            # events when they signal processing, derived otherwise); a batch that
            # times out or drops the connection marks all of ITS files, never the
            # rest of the run.
            records, usable_total = [], 0
            n_batches = (len(corpus) + batch_size - 1) // batch_size
            for bi in range(n_batches):
                chunk = corpus[bi * batch_size:(bi + 1) * batch_size]
                names = {p.name for p in chunk}
                payload = [(str(p), {"doc_id": p.stem}) for p in chunk]
                tb = time.perf_counter_ns()
                failed_why = None
                try:
                    res = await asyncio.wait_for(client.send_files(payload, token),
                                                 timeout=BATCH_TIMEOUT_S)
                except asyncio.TimeoutError:
                    res, failed_why = [], "timeout"
                except Exception as exc:
                    res = []
                    failed_why = f"transport_error: {type(exc).__name__}: {exc}"[:250]
                tb_end = time.perf_counter_ns()
                bspan_ns = tb_end - tb
                ev = {k: v for k, v in event_times.items() if k in names}
                if ev and bspan_ns and (max(ev.values()) - tb) / bspan_ns < 0.5:
                    ev = {}          # upload-only events: fall back to derived
                recs = records_from_batch(chunk, res, tb, mono_offset_ns, ev)
                for r in recs:
                    r["batch_index"] = bi
                    r["batch_size"] = len(chunk)
                    if failed_why and r.get("reason") == "no_response_for_file":
                        r["reason"] = failed_why.split(":")[0]
                        r["error"] = failed_why
                        r["completion_ns"] = tb_end
                records.extend(recs)
                usable_total += len(ev)
                batch_spans.append(bspan_ns / 1e9)
                if (bi + 1) % 10 == 0 or bi + 1 == n_batches:
                    ok_so_far = sum(1 for r in records if r.get("ok"))
                    print(f"[rr] batch {bi + 1}/{n_batches}: "
                          f"{min((bi + 1) * batch_size, len(corpus))}/{len(corpus)} docs, "
                          f"ok={ok_so_far}, last batch {bspan_ns / 1e9:.1f}s"
                          + (f" [{failed_why}]" if failed_why else ""), flush=True)
            span = (time.perf_counter_ns() - t0) / 1e9
            client_observed = usable_total
            bs = sorted(batch_spans)
            print(f"[rr] {n_batches} batches of {batch_size}; client-observed "
                  f"completions for {usable_total}/{len(corpus)} files; batch span "
                  f"p50={bs[len(bs) // 2]:.1f}s max={bs[-1]:.1f}s", flush=True)
        else:
            records = await asyncio.gather(*[one(i, p) for i, p in enumerate(corpus)])
            span = (time.perf_counter_ns() - t0) / 1e9

    # ---- per-node timing, derived after the run (never in the handler) ----
    node_summary = None
    if TRACE_LEVEL:
        node_summary = summarize_flow(flow_raw, t0, span)
        (out / "flow_events.jsonl").write_text("".join(
            json.dumps({"ts_ns": ts, "pipe": pid, "op": op, "component": comp}) + "\n"
            for ts, pid, op, comp in flow_raw))
        (out / "node_timings.json").write_text(json.dumps(node_summary, indent=1))
        print(f"[rr] flow events: {dict(flow_counts)}", flush=True)
        if node_summary.get("nodes"):
            print(f"[rr] {'node':<26}{'calls':>8}{'total s':>10}{'% node':>8}"
                  f"{'p50 ms':>10}{'p95 ms':>10}{'max ms':>11}", flush=True)
            for nm, v in node_summary["nodes"].items():
                print(f"[rr] {nm:<26}{v['calls']:>8}{v['total_s']:>10.1f}"
                      f"{v['pct_of_node_time']:>8.1f}{v['p50_ms']:>10.1f}"
                      f"{v['p95_ms']:>10.1f}{v['max_ms']:>11.1f}", flush=True)
        else:
            print("[rr] WARNING: tracing was ON but no enter/leave pairs were "
                  "captured — flow traces did not arrive", flush=True)

    with open(out / "per_doc.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps({
            "kind": "shot_meta", "arm": "rocketride-docker", "mode": mode,
            "measurement_t0_ns": t0,
            "measurement_t1_ns": t0 + round(span * 1e9),
            "n_docs": len(corpus), "span_s": round(span, 2),
            "timeout_s": TIMEOUT_S,
            "offered_concurrency": offered,
            "client_pool": len(pool),
            "client_pool_cap": None if POOL_MAX >= 10 ** 9 else POOL_MAX,
            "threads_requested": threads,
            "warm_docs": warm_docs,
            "warm_disjoint_from_measured": warm_disjoint,
            "event_actions_seen": dict(event_actions),
            "client_observed_completions": client_observed,
            "submission": ("one whole-corpus send_files batch" if mode == "blast"
                           else f"consecutive send_files batches of {batch_size} "
                                f"documents on one connection" if batch_size
                           else "one send_files per document, closed-loop"),
            "batch_size": batch_size,
            "n_batches": n_batches,
            "batch_spans_s": batch_spans,
            "batch_span_s": ({"min": round(min(batch_spans), 3),
                              "p50": round(sorted(batch_spans)[len(batch_spans) // 2], 3),
                              "max": round(max(batch_spans), 3)}
                             if batch_spans else None),
            "latency_note": ("per-document latency is position within its "
                             "batch of %d (batch submit -> client-observed "
                             "completion)" % batch_size) if batch_size else None,
            "warm_s": round(warm_s, 3) if warm_s is not None else None,
            "configured_concurrency_note":
                "threads_requested is what was ASKED FOR; the engine pool actually "
                "used is not observable from here — never conflate the two.",
            "mono_offset_ns": mono_offset_ns,
            "trace_level": TRACE_LEVEL,
            "profiler_enabled": profile.enabled,
            "unprofiled": not profile.enabled,
            "profiler_capture": "profiler/session.json" if profile.enabled else None,
            "flow_ops_seen": dict(flow_counts) if TRACE_LEVEL else None,
            "node_timings": node_summary,
        }) + "\n")

    # terminate() before disconnect: disconnecting alone leaves the pipeline and
    # its backend running, and a surviving backend holds RSS that starves the
    # next run. Best-effort — a wedged backend may not reap, which is itself
    # worth seeing in the engine log.
    try:
        await client.terminate(token)
    except Exception as exc:
        print(f"[rr] terminate failed: {type(exc).__name__}: {exc}", flush=True)
    for c in pool:
        try:
            await c.disconnect()
        except Exception:
            pass
    ok_n = sum(1 for r in records if r.get("ok"))
    print(f"[rr] done in {span:.1f}s ok={ok_n}/{len(records)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
