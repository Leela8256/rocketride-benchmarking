"""Container resource sampler — runs INSIDE the container, stdlib only.

Emits the exact schema metrics.m7_resources.container_resources() consumes:
  {ts, cpu_total_s, rss_mb_sum, n_procs, n_threads}

WHY NOT pdf1k/proc_sampler.py: that one derives cpu_total_s from /proc/stat,
which is NOT namespaced in Docker and whose fields 1-7 include `idle`. It
therefore reports ~n_host_cpus of "CPU" regardless of load — visible in
GATE50_REPORT.json as langgraph avg_cores 17.81 on an 18-vCPU VM while the
container was capped at 12. Here cpu_total_s comes from the container's OWN
cgroup accounting, which counts only this container's busy CPU and survives
processes that exit mid-run.

  docker exec -i <container> python3 - < cgroup_sampler.py > sampler.jsonl
"""

import json
import os
import sys
import time

PAGE = os.sysconf("SC_PAGE_SIZE")
HZ = os.sysconf("SC_CLK_TCK")


def _cgroup_v2_cpu():
    with open("/sys/fs/cgroup/cpu.stat", "rb") as f:
        for line in f:
            if line.startswith(b"usage_usec"):
                return int(line.split()[1]) / 1e6
    raise KeyError("usage_usec")


def _cgroup_v1_cpu():
    with open("/sys/fs/cgroup/cpuacct/cpuacct.usage", "rb") as f:
        return int(f.read().strip()) / 1e9


def _proc_sum_cpu(procs):
    """Last resort: sum live processes. Loses CPU of exited processes, so it
    can go DOWN between samples — the report flags that rather than hide it."""
    return sum(p["cpu_s"] for p in procs)


def pick_cpu_source():
    for name, fn in (("cgroup_v2", _cgroup_v2_cpu), ("cgroup_v1", _cgroup_v1_cpu)):
        try:
            fn()
            return name, fn
        except Exception:
            continue
    return "proc_sum_fallback", None


def _cgroup_v2_rss():
    """anon = true resident memory. memory.current includes page cache, which
    PDF parsing fills; counting it would inflate RSS on both arms."""
    with open("/sys/fs/cgroup/memory.stat", "rb") as f:
        for line in f:
            if line.startswith(b"anon "):
                return int(line.split()[1]) / 1048576
    raise KeyError("anon")


def sample_procs():
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                parts = f.read().split(b")")[-1].split()
            out.append({
                "pid": int(d),
                "cpu_s": (int(parts[11]) + int(parts[12])) / HZ,
                "rss_mb": int(parts[21]) * PAGE / 1048576,
                "threads": int(parts[17]),
            })
        except Exception:
            continue
    return out


def main():
    interval = float(os.environ.get("SAMPLE_INTERVAL_S", "0.5"))
    max_s = float(os.environ.get("SAMPLE_MAX_S", "1800"))
    source, cpu_fn = pick_cpu_source()
    try:
        _cgroup_v2_rss()
        rss_source = "cgroup_anon"
    except Exception:
        rss_source = "proc_rss_sum"

    # Provenance rides on EVERY sample rather than a leading meta line:
    # metrics.m7_resources.container_resources() reads lines[0] and lines[-1]
    # directly and would KeyError on a line without `ts`. Extra keys are
    # ignored by it, so this is both safe and self-describing.
    started = time.time()
    while time.time() - started < max_s:
        t = time.time()
        procs = sample_procs()
        cpu_s = cpu_fn() if cpu_fn else _proc_sum_cpu(procs)
        rss = _cgroup_v2_rss() if rss_source == "cgroup_anon" else \
            sum(p["rss_mb"] for p in procs)
        sys.stdout.write(json.dumps({
            "ts": round(t, 3),
            "cpu_total_s": round(cpu_s, 3),
            "rss_mb_sum": round(rss, 1),
            "n_procs": len(procs),
            "n_threads": sum(p["threads"] for p in procs),
            "cpu_source": source,
            "rss_source": rss_source,
        }) + "\n")
        sys.stdout.flush()
        time.sleep(max(0.0, interval - (time.time() - t)))


if __name__ == "__main__":
    main()
