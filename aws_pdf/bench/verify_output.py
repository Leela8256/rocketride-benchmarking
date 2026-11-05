"""Look at the ACTUAL output — text and vectors — from both arms.

Everything else in this suite verifies structure and consistency: chunk
counts, 384 dims, L2 norms, hashes matching across runs. All of that would
pass on mojibake, on whitespace, or on semantically degenerate vectors,
because a hash is just as stable for garbage as for prose. Nothing had ever
read the output.

Runs in the bench CLIENT container, same as the drivers.

  python3 verify_output.py <corpus_dir> <doc.pdf> [doc.pdf ...]
"""

import asyncio
import hashlib
import json
import math
import os
import sys
import urllib.request
import uuid
from pathlib import Path

LG_URL = os.environ.get("LG_URL", "http://langgraph:8100")
RR_URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_pdf.pipe"))


def lg_run(pdf: Path):
    boundary = "----v" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{pdf.name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode(),
        pdf.read_bytes(), b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n\r\n{{}}\r\n'.encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"{LG_URL}/v1/process/document-pdf-v1", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())["output"]
    return [c["text"] for c in out["chunks"]], out["vectors"]


def documents_from(result):
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
        for i in result:
            out.extend(documents_from(i))
        return out
    return []


async def rr_run(pdfs):
    from rocketride import RocketRideClient
    pipe = json.loads(PIPE.read_text())
    pipe["project_id"] = str(uuid.uuid4())
    p = Path("/tmp/verify.pipe"); p.write_text(json.dumps(pipe))
    c = RocketRideClient(uri=RR_URI, auth=APIKEY)
    await c.connect()
    tok = (await c.use(filepath=str(p), use_existing=True, ttl=600))["token"]
    out = {}
    for pdf in pdfs:
        up = await asyncio.wait_for(c.send_files([str(pdf)], tok), timeout=300)
        docs = documents_from(up)
        out[pdf.name] = ([d.get("page_content", "") for d in docs],
                         [d.get("embedding") or [] for d in docs])
    try:
        await c.terminate(tok)
    except Exception:
        pass
    await c.disconnect()
    return out


def show(tag, texts, vecs):
    print(f"  {tag}: {len(texts)} chunks, {sum(len(t) for t in texts)} chars, "
          f"{len(vecs)} vectors")
    if not texts:
        return
    t = texts[0]
    print(f"    chunk[0] first 220 chars:\n      {t[:220]!r}")
    if len(texts) > 1:
        print(f"    chunk[-1] last 120 chars:\n      {texts[-1][-120:]!r}")
    printable = sum(c.isprintable() or c in "\n\t" for c in t) / max(len(t), 1)
    letters = sum(c.isalpha() for c in t) / max(len(t), 1)
    print(f"    printable={printable:.3f}  alphabetic={letters:.3f}  "
          f"(mojibake would drop both)")
    v = vecs[0]
    print(f"    vector[0][:6]={[round(x, 5) for x in v[:6]]}")
    print(f"    dim={len(v)}  L2={math.sqrt(sum(x*x for x in v)):.6f}  "
          f"distinct={len(set(v))}  zeros={sum(1 for x in v if x == 0)}")


def main():
    corpus = Path(sys.argv[1])
    names = sys.argv[2:]
    pdfs = [corpus / n for n in names]
    rr = asyncio.run(rr_run(pdfs))
    for pdf in pdfs:
        print("=" * 72)
        print(f"{pdf.name}  ({pdf.stat().st_size/1024:.0f} KB)")
        print("=" * 72)
        lt, lv = lg_run(pdf)
        rt, rv = rr[pdf.name]
        show("LangGraph ", lt, lv)
        show("RocketRide", rt, rv)
        same = lt == rt
        print(f"  TEXT IDENTICAL: {same}")
        if not same and lt and rt:
            for i, (a, b) in enumerate(zip(lt, rt)):
                if a != b:
                    print(f"  first differing chunk: index {i} "
                          f"(lg {len(a)} chars, rr {len(b)} chars)")
                    for j, (x, y) in enumerate(zip(a, b)):
                        if x != y:
                            print(f"    first differing char at {j}: "
                                  f"lg={x!r} (U+{ord(x):04X}) vs "
                                  f"rr={y!r} (U+{ord(y):04X})")
                            print(f"    lg context: {a[max(0,j-60):j+60]!r}")
                            print(f"    rr context: {b[max(0,j-60):j+60]!r}")
                            break
                    else:
                        print(f"    common prefix identical; lengths differ. "
                              f"lg tail={a[len(b):][:80]!r} "
                              f"rr tail={b[len(a):][:80]!r}")
                    break
        # cosine on chunk 0, when both produced identical text
        if same and lv and rv:
            a, b = lv[0], rv[0]
            cos = sum(x*y for x, y in zip(a, b)) / (
                math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)))
            print(f"  cosine(vector[0]) between arms = {cos:.8f}  "
                  f"max|delta| = {max(abs(x-y) for x, y in zip(a, b)):.3e}")
        print()


if __name__ == "__main__":
    main()
