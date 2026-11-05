"""Capture an already-running task (PDF, video, or another pipeline).

Run in a client environment, not in the engine's measured cgroup:
  python profile_task.py --token TOKEN --seconds 60 --output results/diagnostic
The workload must be supplied independently during this window.
"""

import argparse
import asyncio
import math
import os

from profile_capture import ProfileCapture


async def main(args):
    from rocketride import RocketRideClient
    client = RocketRideClient(uri=args.uri, auth=os.environ.get("ROCKETRIDE_APIKEY", "local-dev"))
    await client.connect()
    try:
        async with ProfileCapture(client, args.token, args.output, args.seconds):
            await asyncio.sleep(args.seconds)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--output", required=True)
    parser.add_argument("--uri", default=os.environ.get("ROCKETRIDE_URI", "ws://127.0.0.1:5565/task/service"))
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    asyncio.run(main(args))
