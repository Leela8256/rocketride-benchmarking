"""Capture pass — ONE video through the pipe, saving the FULL response.

The sizing driver deliberately keeps only chunk hashes and vector norms;
this saves everything (page_content + complete 384-dim embeddings +
metadata) so the vision path can be cross-checked offline against
independently extracted frames and independently recomputed embeddings.

  python capture_one.py <video_path> <out_dir>
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bench_video import documents_from  # same unwrapping as the sizing runs

URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_video_detect.pipe"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT_S", "3600"))


async def main():
    import uuid
    from rocketride import RocketRideClient

    video, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    pipe = json.loads(PIPE_SRC.read_text())
    pipe["project_id"] = str(uuid.uuid4())
    pipe_path = out / "pipeline.pipe"
    pipe_path.write_text(json.dumps(pipe))

    client = RocketRideClient(uri=URI, auth=APIKEY)
    await client.connect()
    used = await client.use(filepath=str(pipe_path), use_existing=True, ttl=7200)
    token = used["token"]
    print(f"[cap] pipeline up ({PIPE_SRC.name}), sending {video.name}", flush=True)
    resp = await asyncio.wait_for(
        client.send_files([(str(video), {"doc_id": video.stem})], token),
        timeout=TIMEOUT_S)
    docs = documents_from(resp)
    (out / "capture_docs.json").write_text(json.dumps([
        {"page_content": d.get("page_content", ""),
         "embedding": d.get("embedding"),
         "metadata": d.get("metadata")} for d in docs]))
    print(f"[cap] saved {len(docs)} chunks with FULL embeddings "
          f"-> capture_docs.json", flush=True)
    try:
        await client.terminate(token)
    finally:
        await client.disconnect()
    if not docs:
        raise SystemExit("CAPTURE FAILED: no documents")


if __name__ == "__main__":
    asyncio.run(main())
