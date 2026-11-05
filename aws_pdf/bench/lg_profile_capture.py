"""Client-side capture control, outside the measured LangGraph cgroup."""

import json
import math
import os
import threading
import time
import urllib.request
from pathlib import Path


class LangGraphCapture:
    def __init__(self, base, output):
        self.base = base
        self.output = Path(output) / "profiler"
        self.seconds = float(os.environ.get("LG_PROFILE_SECONDS", "0"))
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("LG_PROFILE_SECONDS must be finite and nonnegative")
        self.enabled = self.seconds > 0
        self.started = False
        self.timer = None
        self.error = None
        self.lock = threading.Lock()
        self.meta = {"run_kind": "diagnostic", "clock": "wall",
                     "requested_seconds": self.seconds,
                     "session": f"lg-bench-{time.time_ns()}", "complete": False}

    def save(self, name, value):
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / name).write_text(json.dumps(value, indent=2) + "\n")

    def call(self, operation):
        request = urllib.request.Request(
            self.base + "/__bench/profile/" + operation,
            data=json.dumps({"session": self.meta["session"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)

    def __enter__(self):
        if not self.enabled:
            return self
        if self.output.exists():
            raise FileExistsError(f"refusing to overwrite {self.output}")
        self.save("session.json", self.meta)
        try:
            self.meta["start"] = self.call("start")
            self.started = True
            self.meta.update(start_epoch_ns=time.time_ns(),
                             start_monotonic_ns=time.perf_counter_ns())
            self.save("session.json", self.meta)
            self.timer = threading.Timer(self.seconds, self.deadline)
            self.timer.daemon = True
            self.timer.start()
            return self
        except Exception as exc:
            self.meta["error"] = repr(exc)
            self.save("session.json", self.meta)
            raise

    def stop(self):
        with self.lock:
            if self.started:
                self.meta["stop"] = self.call("stop")
                self.started = False
                self.meta.update(stop_epoch_ns=time.time_ns(),
                                 stop_monotonic_ns=time.perf_counter_ns())
                self.save("session.json", self.meta)

    def deadline(self):
        try:
            self.meta["safety_deadline_reached"] = True
            self.stop()
        except Exception as exc:
            self.error = exc

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        if exc:
            self.meta["workload_error"] = repr(exc)
        try:
            if self.timer:
                self.timer.cancel()
            self.stop()
            if self.timer:
                self.timer.join()
            if self.error:
                raise self.error
            data = self.call("report")
            (self.output / "report.txt").write_text(data["report"])
            self.save("functions.json", data["functions"])
            self.save("threads.json", data["threads"])
            for tid, rows in data["per_thread"].items():
                self.save(f"thread-{tid}.json", rows)
            self.meta["complete"] = True
        except Exception as error:
            self.meta["error"] = repr(error)
            if exc is None:
                raise
        finally:
            self.save("session.json", self.meta)
        return False
