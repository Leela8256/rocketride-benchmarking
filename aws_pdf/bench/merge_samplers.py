"""Merge supplementary cgroup sampler streams into a rep's sampler.jsonl.

Why: run/matched_run.sh used to start its in-container sampler with
SAMPLE_MAX_S=7200, so a rep that ran longer than two hours (the 2026-09-16
b16/b32/b64 cells ran 3.4-5.1 h) lost CPU/RSS coverage for its tail. A
supervisor on the box ran the SAME sampler (bench/cgroup_sampler.py, same
cgroup counters, same container) without the cap; this merges those streams so
metrics.m7_resources.window() sees the whole window.

The runner's stream is AUTHORITATIVE wherever it has samples: two samplers
reading one cumulative counter a fraction of a millisecond apart produce
values that interleave out of order by a few hundredths of a CPU-second, and
mixing them would make the merged series non-monotonic for no gain. A
supplementary sample is therefore used only where the runner has none (the
uncovered tail, or a gap). What survives is one continuous series.

A genuine counter RESET -- a supplementary stream from a different container
start, whose counter begins near zero -- is a different matter and must never
be merged silently, so a drop larger than RESET_DROP_S aborts.

  python3 bench/merge_samplers.py <rep_dir> <extra.jsonl> [<extra.jsonl> ...]
"""

import json
import shutil
import sys
from bisect import bisect_left
from pathlib import Path

# Half a sample interval (the sampler ticks at 0.5 s): a supplementary sample
# this close to a runner sample is the same instant measured twice.
GRID_TOLERANCE_S = 0.25
# A cumulative cgroup counter never falls. A drop beyond this is a container
# restart, not sampler jitter -- and merging across it would be nonsense.
RESET_DROP_S = 60.0


def load(p: Path):
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ts" in r and "cpu_total_s" in r:
            out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def main():
    rep = Path(sys.argv[1])
    extras = [Path(x) for x in sys.argv[2:]]
    target = rep / "sampler.jsonl"
    keep = rep / "sampler.runner.jsonl"
    if not keep.exists():
        shutil.copy2(target, keep)          # original, untouched, kept forever
    base = load(keep)
    if not base:
        raise SystemExit(f"{keep}: no usable samples")

    rows = list(base)
    counts = {"runner": len(base)}
    added_detail = {}
    for e in extras:
        cand = load(e)
        counts[e.name] = len(cand)
        ts = [r["ts"] for r in rows]
        added = []
        for r in cand:
            i = bisect_left(ts, r["ts"])
            near = False
            for j in (i - 1, i):
                if 0 <= j < len(ts) and abs(ts[j] - r["ts"]) <= GRID_TOLERANCE_S:
                    near = True
                    break
            if not near:
                added.append(r)
        rows = sorted(rows + added, key=lambda r: r["ts"])
        added_detail[e.name] = len(added)

    # One continuous, non-decreasing series, or an explained abort.
    for x, y in zip(rows, rows[1:]):
        drop = x["cpu_total_s"] - y["cpu_total_s"]
        if drop > RESET_DROP_S:
            raise SystemExit(
                f"ABORT: cpu_total_s falls {drop:.1f} s at ts={y['ts']} "
                f"({x['cpu_total_s']:.1f} -> {y['cpu_total_s']:.1f}). That is a "
                f"container restart, not sampler jitter; these streams are from "
                f"different engine runs and must not be merged.")
    jitter = [round(x["cpu_total_s"] - y["cpu_total_s"], 3)
              for x, y in zip(rows, rows[1:]) if y["cpu_total_s"] < x["cpu_total_s"]]

    with open(target, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    side = {
        "merged_from": counts,
        "samples_added_per_stream": added_detail,
        "n_merged": len(rows),
        "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
        "residual_negative_steps": len(jitter),
        "max_negative_step_cpu_s": max(jitter) if jitter else 0.0,
        "policy": (f"runner stream authoritative; a supplementary sample is used only "
                   f"where no runner sample lies within {GRID_TOLERANCE_S}s. Abort if "
                   f"the counter drops more than {RESET_DROP_S}s (container restart)."),
        "note": "runner stream was capped at SAMPLE_MAX_S=7200 (fixed since); "
                "supplementary stream(s) are the same sampler on the same container "
                "from the box supervisor. Original kept as sampler.runner.jsonl.",
    }
    (rep / "sampler_merge.json").write_text(json.dumps(side, indent=1))
    print(json.dumps(side, indent=1))


if __name__ == "__main__":
    main()
