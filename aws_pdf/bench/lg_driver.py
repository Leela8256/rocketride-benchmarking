"""LangGraph smoke driver — N docs, closed-loop sequential, stdlib only.

Writes per_doc.jsonl in the schema metrics/ consumes, including the full LG
record contract from metrics.m0_correctness.REQUIRED_TRUE["lg"]:
identity_ok, sha_header_ok, vectors_finite.

Sequential on purpose: closed-loop gives TRUE SERVICE LATENCY (metrics/README
M2). A blast run would give batch-position latency and must be labeled so.

Modes: seq | blast | c<N> | b<B>.

b<B> is the BATCHED-SUBMISSION analogue of the RocketRide driver's b<B>: the
corpus is processed in consecutive waves of B documents, and wave k+1 is not
submitted until every document of wave k has returned. The two arms do not
share a wire protocol here and must never be described as if they did --
RocketRide sends ONE `send_files` call carrying B files and the engine
schedules them internally; LangGraph has no batch endpoint, so a wave is B
concurrent HTTP requests released together and joined at a barrier. What the
two share, and what makes the cells comparable, is the SHAPE of the offered
load: at most B documents in flight, and a wave boundary that waits for the
slowest member. Per-document time is measured from wave release to that
document's own completion on both arms, so it carries intra-wave queueing on
both and is labelled batch-position latency on both.

  python3 lg_smoke_driver.py <corpus_dir> <out_dir> [n_docs]
"""

import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = os.environ.get("LG_URL", "http://langgraph:8100")
PIPELINE = "document-pdf-v1"
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "3600"))
EMBED_DIM = 384


def post_pdf(pdf: Path, rid: str, blob: bytes):
    """`blob` is passed in, never re-read here: the file read must happen
    before the latency clock starts or local disk I/O lands in M2."""
    boundary = "----smoke" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{pdf.name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
        blob,
        b"\r\n",
    ]
    for name, value in (("options", "{}"), ("request_id", rid)):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{BASE}/v1/process/{PIPELINE}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        # Lowercase the keys: Starlette normalises response header names, so
        # the wire carries `x-output-sha256`. A plain dict(r.headers) loses
        # urllib's case-insensitivity and every sha check would silently
        # return None -> ok=False on every doc, for no real reason.
        # aws/smoke.sh hits the same thing with tolower($1).
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def one(pdf: Path):
    rid = uuid.uuid4().hex
    blob = pdf.read_bytes()
    rec = {
        "doc": pdf.name,
        "arm": "langgraph-docker",
        "ok": False,
        "input_sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "submit_ns": time.perf_counter_ns(),
    }
    try:
        raw, hdrs = post_pdf(pdf, rid, blob)
        rec["completion_ns"] = time.perf_counter_ns()

        env = json.loads(raw)
        out = env.get("output") or {}
        chunks = [c["text"] for c in out.get("chunks", [])]
        vectors = out.get("vectors") or []

        rec["identity_ok"] = env.get("request_id") == rid
        # canonical_sha256() is plain sha256 of the response body bytes
        # (service/canonical.py), so this is a true end-to-end integrity check.
        rec["sha_header_ok"] = (
            hdrs.get("x-output-sha256") == hashlib.sha256(raw).hexdigest()
        )
        rec["vectors_finite"] = bool(vectors) and all(
            math.isfinite(x) for v in vectors for x in v
        )
        rec["n_chunks"] = len(chunks)
        rec["total_chars"] = sum(len(c) for c in chunks)
        rec["chunk_sha256"] = [
            hashlib.sha256(c.encode("utf-8")).hexdigest() for c in chunks
        ]
        rec["server_timing"] = hdrs.get("server-timing")

        dims = {len(v) for v in vectors}
        rec["vector_dim"] = dims.pop() if len(dims) == 1 else None
        if vectors and rec["vectors_finite"]:
            norms = [math.sqrt(sum(x * x for x in v)) for v in vectors]
            rec["l2_norms_minmax"] = [round(min(norms), 6), round(max(norms), 6)]
        else:
            rec["l2_norms_minmax"] = None

        # ok mirrors the M0 contract: identity + integrity + real content.
        rec["ok"] = bool(
            rec["identity_ok"] and rec["sha_header_ok"] and rec["vectors_finite"]
            and chunks and rec["vector_dim"] == EMBED_DIM
            and len(rec["chunk_sha256"]) == len(chunks)
        )
        if rec["ok"]:
            rec["reason"] = "completed"
        elif not chunks:
            rec["reason"] = "no_documents"
        else:
            rec["reason"] = "completion_proof_missing"
    except urllib.error.HTTPError as exc:
        rec["completion_ns"] = time.perf_counter_ns()
        body = exc.read()[:300].decode(errors="replace")
        rec["reason"] = f"http_{exc.code}"
        # M5 distinguishes SERVER-surfaced failure from client-inferred; it
        # keys on http_status, so record the code as a field, not just text.
        rec["http_status"] = exc.code
        rec["error"] = body
    except Exception as exc:
        rec["completion_ns"] = time.perf_counter_ns()
        rec["reason"] = "transport_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:250]
    return rec


def main():
    corpus_dir = Path(sys.argv[1])
    out = Path(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    # seq | blast | c<N>.  blast = whole backlog submitted at once, the
    # framework schedules it. c<N> = closed loop holding N in flight.
    mode = sys.argv[4] if len(sys.argv) > 4 else "seq"
    # MATCHED WARM-START. This arm previously received none while RocketRide
    # got 25 real documents, and provenance still claimed both were warmed --
    # an asymmetry the audit artifact actively misreported.
    warm_docs = int(sys.argv[5]) if len(sys.argv) > 5 else 0

    # Warm-up documents are DISJOINT from measured ones: a measured doc that
    # was already warmed is cache-hot and no longer comparable to its peers.
    # The corpus dir holds N measured + EXTRA warm; if EXTRA was not fetched,
    # fall back to the head of the corpus and say so.
    all_pdfs = sorted(corpus_dir.glob("*.pdf"))
    corpus = all_pdfs[:n]
    warm_set = all_pdfs[n:n + warm_docs]
    warm_disjoint = len(warm_set) == warm_docs
    if warm_docs > 0 and not warm_disjoint:
        warm_set = corpus[:warm_docs]
        print(f"  WARNING: only {len(all_pdfs)} PDFs for n={n}+warm={warm_docs};"
              f" warm-up REUSES measured docs (not disjoint)", flush=True)
    if not corpus:
        raise SystemExit(f"no PDFs in {corpus_dir}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({"docs": [p.name for p in corpus], "n": len(corpus)})
    )

    batch_size = None
    if mode == "seq":
        offered = 1
    elif mode == "blast":
        offered = len(corpus)
    elif mode.startswith("c") and mode[1:].isdigit():
        offered = int(mode[1:])
    elif mode.startswith("b") and mode[1:].isdigit() and int(mode[1:]) > 0:
        batch_size = int(mode[1:])
        offered = min(batch_size, len(corpus))
    else:
        raise SystemExit(f"bad mode {mode!r}: expected seq | blast | c<N> | b<B>")

    # Sampler timestamps are wall-clock epoch; per-doc timestamps are
    # monotonic. This offset is what lets m7_resources.window() slice the
    # sampler to EXACTLY the throughput window.
    mono_offset_ns = time.time_ns() - time.perf_counter_ns()

    warm_s = None
    if warm_docs > 0:
        tw = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=min(warm_docs, 8)) as pool:
            list(pool.map(one, warm_set))
        warm_s = (time.perf_counter_ns() - tw) / 1e9
        print(f"  warm-start: {warm_docs} docs in {warm_s:.1f}s (excluded)",
              flush=True)

    from lg_profile_capture import LangGraphCapture
    profile = LangGraphCapture(BASE, out)
    with profile:
        t0 = time.perf_counter_ns()
        records = []
        n_batches = None
        batch_spans = []
        if batch_size:
            # Consecutive waves of B, joined at a barrier. A fresh pool per wave IS
            # the barrier: its context manager will not exit until every request of
            # the wave has returned, so wave k+1 cannot start early. Each request
            # carries its own measured submit/completion, and a wave that fails
            # wholesale marks only ITS documents.
            n_batches = (len(corpus) + batch_size - 1) // batch_size
            for bi in range(n_batches):
                chunk = corpus[bi * batch_size:(bi + 1) * batch_size]
                tb = time.perf_counter_ns()
                with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
                    recs = list(pool.map(one, chunk))
                bspan = (time.perf_counter_ns() - tb) / 1e9
                for r in recs:
                    r["batch_index"] = bi
                    r["batch_size"] = len(chunk)
                records.extend(recs)
                batch_spans.append(bspan)
                if (bi + 1) % 10 == 0 or bi + 1 == n_batches:
                    ok_so_far = sum(1 for r in records if r.get("ok"))
                    print(f"  batch {bi + 1}/{n_batches}: "
                          f"{min((bi + 1) * batch_size, len(corpus))}/{len(corpus)} docs, "
                          f"ok={ok_so_far}, last batch {bspan:.1f}s", flush=True)
            bs = sorted(batch_spans)
            print(f"  {n_batches} batches of {batch_size}; batch span "
                  f"p50={bs[len(bs) // 2]:.1f}s max={bs[-1]:.1f}s", flush=True)
        elif offered == 1:
            for i, pdf in enumerate(corpus, 1):
                rec = one(pdf)
                records.append(rec)
                print(f"  [{i}/{len(corpus)}] {pdf.name}: ok={rec['ok']} "
                      f"chunks={rec.get('n_chunks')} "
                      f"{(rec['completion_ns'] - rec['submit_ns']) / 1e6:.0f} ms",
                      flush=True)
        else:
            done = [0]
            lock = threading.Lock()

            def run(pdf):
                rec = one(pdf)
                with lock:
                    done[0] += 1
                    if done[0] % 25 == 0 or done[0] == len(corpus):
                        print(f"  [{done[0]}/{len(corpus)}]", flush=True)
                return rec

            with ThreadPoolExecutor(max_workers=offered) as pool:
                records = list(pool.map(run, corpus))
        span = (time.perf_counter_ns() - t0) / 1e9

    with open(out / "per_doc.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps({
            "kind": "shot_meta", "arm": "langgraph-docker", "mode": mode,
            "n_docs": len(corpus), "span_s": round(span, 2),
            "measurement_t0_ns": t0,
            "measurement_t1_ns": t0 + round(span * 1e9),
            "timeout_s": TIMEOUT_S,
            "warm_docs": warm_docs,
            "warm_disjoint_from_measured": warm_disjoint,
            "warm_s": round(warm_s, 3) if warm_s is not None else None,
            "offered_concurrency": offered,
            "submission": (
                f"consecutive waves of {batch_size} concurrent HTTP requests, "
                f"joined at a barrier (LangGraph has no batch endpoint; this is "
                f"the offered-load analogue of RocketRide's b{batch_size}, NOT "
                f"the same wire protocol)" if batch_size
                else "one HTTP request per document"),
            "batch_size": batch_size,
            "n_batches": n_batches,
            "batch_spans_s": batch_spans,
            "batch_span_s": ({"min": round(min(batch_spans), 3),
                              "p50": round(sorted(batch_spans)[len(batch_spans) // 2], 3),
                              "max": round(max(batch_spans), 3)}
                             if batch_spans else None),
            "latency_basis": ("wave release -> this document's own completion "
                              "(measured per request; includes intra-wave queueing)"
                              if batch_size else
                              "per-request round-trip (measured)"),
            # What the service was TOLD vs what it actually runs: /meta reports
            # EXECUTOR_WORKERS, but nodes.py uses LangGraph's default executor,
            # width min(32, os.cpu_count()+4) -- and cpu_count() sees host
            # cores, not the cgroup quota. Recorded, never assumed equal.
            "configured_concurrency_note":
                "LG uses default executor min(32, cpu_count+4); /meta's "
                "executor_workers is reported but inert",
            "profiler_enabled": profile.enabled,
            "unprofiled": not profile.enabled,
            "profiler_capture": "profiler/session.json" if profile.enabled else None,
            "mono_offset_ns": mono_offset_ns,
        }) + "\n")
    ok = sum(1 for r in records if r.get("ok"))
    print(f"done in {span:.1f}s  mode={mode} offered={offered}  "
          f"ok={ok}/{len(records)} -> {out / 'per_doc.jsonl'}")


if __name__ == "__main__":
    main()
