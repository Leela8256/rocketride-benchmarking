"""Task-census probe: create exactly N RocketRide tasks (matched-posture
style — distinct project_ids, no use_existing, no threads=, ttl=0) and hold
them for HOLD_S seconds so the runner can snapshot the engine container's
process table. Used to learn what a task process looks like (cmdline,
parent, environ) and to validate the census rule before a measured run.

  python3 probe_task_census.py [N] [HOLD_S]
Env: ROCKETRIDE_URI, ROCKETRIDE_APIKEY, BENCH_PIPE
"""

import asyncio
import json
import os
import sys
from pathlib import Path

URI = os.environ.get("ROCKETRIDE_URI", "ws://rocketride-matched:5565/task/service")
APIKEY = os.environ.get("ROCKETRIDE_APIKEY", "local-dev")
PIPE_SRC = Path(os.environ.get("BENCH_PIPE", "/pipe/benchmark_video_detect.pipe"))


async def main():
    from rocketride import RocketRideClient
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    base = json.loads(PIPE_SRC.read_text())
    out = Path("/results/probe_pipes")
    out.mkdir(parents=True, exist_ok=True)
    clients, tokens = [], []
    for k in range(n):
        p = dict(base)
        p["project_id"] = f"probe-census-{k:02d}"
        pp = out / f"probe_{k:02d}.pipe"
        pp.write_text(json.dumps(p))
        c = RocketRideClient(uri=URI, auth=APIKEY)
        await c.connect()
        u = await c.use(filepath=str(pp), ttl=0)
        clients.append(c)
        tokens.append(u["token"])
        print(f"PROBE task {k} created (token len {len(u['token'])})", flush=True)
        await asyncio.sleep(2)
    print(f"PROBE holding {n} task(s) for {hold}s", flush=True)
    await asyncio.sleep(hold)
    for c, t in zip(clients, tokens):
        try:
            await c.terminate(t)
        except Exception as exc:
            print(f"PROBE terminate: {type(exc).__name__}", flush=True)
        await c.disconnect()
    print("PROBE done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
