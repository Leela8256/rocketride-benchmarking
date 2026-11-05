"""Multi-process entrypoint for the MATCHED posture: N independent
single-worker uvicorn processes, one port each (LG_PORT_BASE + i).

Not ``uvicorn --workers N``: kernel connection balancing across workers on
one port can skew badly; one port per process makes the client's endpoint
mapping deterministic. Each worker independently loads one FastAPI app,
one compiled graph, one RF-DETR and one MiniLM singleton.

Forwards SIGTERM/SIGINT to every child; exits nonzero (killing the rest)
the moment any worker dies, so the container fails visibly. Worker logs
are inherited, not hidden.
"""

import os
import signal
import subprocess
import sys
import time

N = int(os.environ.get("LG_WORKERS", "8"))
BASE = int(os.environ.get("LG_PORT_BASE", "8201"))


def main():
    procs = []
    for i in range(N):
        port = BASE + i
        env = dict(os.environ, LG_PORT=str(port), LG_WORKER_INDEX=str(i))
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "service:app",
                              "--host", "0.0.0.0", "--port", str(port),
                              "--workers", "1", "--log-level", "info"], env=env)
        procs.append(p)
        print(f"[serve8] worker {i} pid={p.pid} port={port}", flush=True)

    def forward(sig, _frame):
        print(f"[serve8] signal {sig}: stopping {len(procs)} workers", flush=True)
        for p in procs:
            if p.poll() is None:
                p.send_signal(sig)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    rc = 0
    while True:
        for i, p in enumerate(procs):
            r = p.poll()
            if r is not None:
                print(f"[serve8] worker {i} pid={p.pid} EXITED rc={r} — failing container", flush=True)
                rc = r or 1
                for q in procs:
                    if q.poll() is None:
                        q.terminate()
                for q in procs:
                    try:
                        q.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        q.kill()
                sys.exit(rc)
        time.sleep(1)


if __name__ == "__main__":
    main()
