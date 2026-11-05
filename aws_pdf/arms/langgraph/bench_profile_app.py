"""Optional benchmark diagnostics around the unchanged service app.

Use the same derived image for baseline and diagnostics. With control off,
Yappi is not imported and no diagnostic routes are registered. Never deploy
this benchmark-only entry point as a public service.
"""

import os

from service.app import app

if os.environ.get("BENCH_PROFILE_CONTROL") == "1":
    import io
    import threading
    import time
    import yappi
    from fastapi import HTTPException

    lock = threading.RLock()
    capture = {}

    def name_thread():
        thread = threading._active.get(threading.get_ident())
        return thread.name if thread else None

    def require_session(session):
        if session != capture.get("session"):
            raise HTTPException(409, "profile session does not match")

    def function_rows(stats):
        return [{"index": s.index, "name": s.name, "file": s.module,
                 "line": s.lineno, "ncalls": s.ncall,
                 "cumtime": s.ttot, "tottime": s.tsub,
                 "children": [{"index": c.index, "ncalls": c.ncall,
                               "cumtime": c.ttot, "tottime": c.tsub}
                              for c in s.children]}
                for s in stats]

    @app.post("/__bench/profile/start")
    def start_profile(body: dict):
        with lock:
            if yappi.is_running():
                raise HTTPException(409, "a profile is already active")
            session = body.get("session")
            if not isinstance(session, str) or not session:
                raise HTTPException(400, "session is required")
            yappi.clear_stats()
            capture.clear()
            capture.update(session=session, active=True,
                           start_epoch_ns=time.time_ns())
            yappi.set_clock_type("wall")
            yappi.set_context_name_callback(name_thread)
            yappi.start(builtins=True, profile_threads=True)
            return {"status": "started", "session": session,
                    "start_epoch_ns": capture["start_epoch_ns"]}

    @app.post("/__bench/profile/stop")
    def stop_profile(body: dict):
        with lock:
            require_session(body.get("session"))
            if capture.get("active"):
                yappi.stop()
                capture["active"] = False
                capture["stop_epoch_ns"] = time.time_ns()
                capture["stats"] = yappi.get_func_stats()
                capture["threads"] = list(yappi.get_thread_stats())
                capture["thread_stats"] = {
                    t.id: yappi.get_func_stats(ctx_id=t.id)
                    for t in capture["threads"]}
            return {"status": "completed", "session": capture["session"],
                    "runtime": (capture["stop_epoch_ns"] - capture["start_epoch_ns"]) / 1e9}

    @app.post("/__bench/profile/report")
    def report_profile(body: dict):
        with lock:
            require_session(body.get("session"))
            if capture.get("active") or "stats" not in capture:
                raise HTTPException(409, "stop profiling before exporting")
            buf = io.StringIO()
            capture["stats"].sort("ttot").print_all(out=buf)
            return {
                "report": buf.getvalue(),
                "functions": function_rows(capture["stats"]),
                "threads": [{"id": t.id, "name": t.name, "tid": t.tid,
                             "ttot": t.ttot, "sched_count": t.sched_count}
                            for t in capture["threads"]],
                "per_thread": {str(k): function_rows(v)
                               for k, v in capture["thread_stats"].items()},
            }
