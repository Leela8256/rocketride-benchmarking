"""Opt-in task profiling; diagnostic captures never represent baseline speed.

Uses the documented DAP commands through Client.call so the benchmark can
keep its existing upload SDK. Start after warm-up; export before terminate.
"""

import asyncio
import json
import math
import os
import time
from pathlib import Path


class ProfileCapture:
    def __init__(self, client, token, output, seconds=0):
        self.client, self.token = client, token
        self.output = Path(output) / "profiler"
        self.seconds = float(seconds)
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("RR_PROFILE_SECONDS must be finite and nonnegative")
        self.enabled = self.seconds > 0
        self.started = False
        self.timer = None
        self.stop_lock = asyncio.Lock()
        self.meta = {"run_kind": "diagnostic", "clock": "wall",
                     "requested_seconds": self.seconds, "target": token,
                     "session": f"bench-{time.time_ns()}", "complete": False}

    @classmethod
    def from_env(cls, client, token, output):
        return cls(client, token, output, os.environ.get("RR_PROFILE_SECONDS", "0"))

    def save(self, name, value):
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / name).write_text(json.dumps(value, indent=2) + "\n")

    async def call(self, operation, **kwargs):
        result = await self.client.call(f"rrext_cprofile_{operation}",
                                        target=self.token, **kwargs)
        if result.get("error") or result.get("status") == "error":
            raise RuntimeError(f"profiler {operation}: {result}")
        return result

    async def __aenter__(self):
        if not self.enabled:
            return self
        # Write intent first, so even a failed capture cannot look unprofiled.
        if self.output.exists():
            raise FileExistsError(f"refusing to overwrite {self.output}")
        self.save("session.json", self.meta)
        try:
            status = await self.call("status")
            if status.get("active"):
                raise RuntimeError("target already has an active profiling session")
            result = await self.call("start", session=self.meta["session"])
            if result.get("status") != "started":
                raise RuntimeError(f"unexpected profiler start response: {result}")
            self.started = True
            self.meta.update(start=result, start_epoch_ns=time.time_ns(),
                             start_monotonic_ns=time.perf_counter_ns())
            self.save("session.json", self.meta)
            self.timer = asyncio.create_task(self._deadline())
            return self
        except Exception as exc:
            self.meta["error"] = str(exc)
            self.save("session.json", self.meta)
            raise

    async def _deadline(self):
        await asyncio.sleep(self.seconds)
        await self.stop()

    async def stop(self):
        async with self.stop_lock:
            if not self.started:
                return
            status = await self.call("status")
            if not status.get("active") or status.get("session") != self.meta["session"]:
                self.started = False
                raise RuntimeError("profiling session was stopped or replaced externally")
            result = await self.call("stop")
            if result.get("status") != "completed":
                raise RuntimeError(f"unexpected profiler stop response: {result}")
            self.started = False
            self.meta.update(stop=result, stop_epoch_ns=time.time_ns(),
                             stop_monotonic_ns=time.perf_counter_ns())
            self.save("session.json", self.meta)

    async def __aexit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        if exc is not None:
            self.meta["workload_error"] = repr(exc)
        try:
            # Let an in-progress stop finish; wake a sleeping timer by stopping
            # here and cancel it only after the guarded stop has completed.
            await self.stop()
            if self.timer and not self.timer.done():
                self.timer.cancel()
            if self.timer:
                try:
                    await self.timer
                except asyncio.CancelledError:
                    pass
            threads = await self.call("threads")
            self.save("threads.json", threads)
            report = await self.call("report")
            (self.output / "report.txt").write_text(report["report"])
            self.save("tree.json", await self.call("report_tree", min_pct=0.1,
                                                   max_depth=50, include_system=True))
            # Export every thread, not just the largest wall-time threads:
            # blocked/idle threads can otherwise hide the useful worker.
            for thread in threads.get("threads", []):
                self.save(f"thread-{thread['id']}.json", await self.call(
                    "report_tree", thread=thread["id"], min_pct=0.1,
                    max_depth=50, include_system=True))
            self.meta["complete"] = True
        except Exception as error:
            self.meta["error"] = repr(error)
            if exc is None:
                raise
        finally:
            if self.timer and not self.timer.done():
                self.timer.cancel()
                await asyncio.gather(self.timer, return_exceptions=True)
            self.save("session.json", self.meta)
        return False
