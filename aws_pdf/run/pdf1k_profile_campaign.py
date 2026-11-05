"""Frozen-image PDF campaign: one profiled run per framework by default.

Runs one arm at a time, alternating baseline order, using existing drivers,
metrics and provenance. Launch under nohup on the client CPU set. Containers
are private to this campaign; public benchmark services are stopped first.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARM_CPUS = "0-11,16-27"
CLIENT_CPUS = "12-15,28-31"
NET = "pdf1k-profiler"
NAMES = {"rr": "pdf1k-rocketride", "lg": "pdf1k-langgraph", "tika": "pdf1k-tika"}


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(args):
    return command(args, capture_output=True, text=True).stdout.strip()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def inspect(name):
    return json.loads(output(["docker", "inspect", name]))[0]


class Sampler:
    """Read container counters from the host; sampler cost stays outside arms."""
    def __init__(self, container, path):
        self.stop_event = threading.Event()
        self.errors = []
        pid = inspect(container)["State"]["Pid"]
        groups = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        group = next(line[3:] for line in groups if line.startswith("0::"))
        self.group = Path("/sys/fs/cgroup") / group.lstrip("/")
        self.path = path
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            with self.path.open("w") as stream:
                while not self.stop_event.is_set():
                    cpu = dict(line.split() for line in (self.group / "cpu.stat").read_text().splitlines())
                    mem = dict(line.split() for line in (self.group / "memory.stat").read_text().splitlines())
                    procs = (self.group / "cgroup.procs").read_text().split()
                    threads = len((self.group / "cgroup.threads").read_text().split())
                    per_process = []
                    for pid in procs:
                        try:
                            stat = Path(f"/proc/{pid}/stat").read_text()
                            fields = stat.rsplit(")", 1)[1].split()
                            per_process.append({"pid": int(pid), "ppid": int(fields[1]),
                                "name": stat.split("(", 1)[1].rsplit(")", 1)[0],
                                "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                                "threads": int(fields[17])})
                        except (FileNotFoundError, ProcessLookupError, PermissionError):
                            pass  # short-lived children may exit between reads
                    stream.write(json.dumps({"ts": time.time(),
                        "cpu_total_s": int(cpu["usage_usec"]) / 1e6,
                        "rss_mb_sum": int(mem["anon"]) / 1048576,
                        "n_procs": len(procs), "n_threads": threads,
                        "processes": per_process,
                        "cpu_source": "host_cgroup_v2", "rss_source": "cgroup_anon"}) + "\n")
                    stream.flush()
                    self.stop_event.wait(0.5)
        except Exception as exc:
            self.errors.append(repr(exc))

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.errors:
            raise RuntimeError(f"sampler failed: {self.errors}")


def env_args(values):
    return [part for k, v in values.items() for part in ("-e", f"{k}={v}")]


def run_service(arm, images, profile, frozen, hf_cache):
    env = {"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false"}
    args = ["docker", "run", "-d", "--name", NAMES[arm], "--network", NET,
            "--cpuset-cpus", ARM_CPUS]
    if arm in ("lg", "rr"):
        args += ["-v", f"{hf_cache}:/bench-hf:ro"]
        env.update(HF_HOME="/bench-hf", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if arm == "lg":
        env.update(EXTRACTOR="tika", TIKA_URL="http://pdf1k-tika:9998",
                   HF_HUB_OFFLINE="1", PIPELINES="document-pdf-v1",
                   TORCH_THREADS="1", TORCH_INTEROP_THREADS="1",
                   EXECUTOR_WORKERS="4", MAX_INFLIGHT_REQUESTS="8",
                   REQUEST_TIMEOUT_SECONDS="300", MAX_UPLOAD_BYTES="104857600",
                   BENCH_PROFILE_CONTROL="1" if profile else "0")
    elif arm == "tika":
        args += ["-v", f"{frozen / 'tika-config'}:/tika-config:ro"]
    else:
        env.update(RR_HOST="0.0.0.0", RR_PORT="5565", ROCKETRIDE_APIKEY="local-dev")
    args += env_args(env) + [images[arm]]
    if arm == "tika":
        args += ["-c", "/tika-config/tika-config.xml"]
    command(args, stdout=subprocess.DEVNULL)


def ready(arm):
    port, path = (8100, "/health/ready") if arm == "lg" else (5565, "/version")
    code = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}{path}',timeout=3).read().decode())"
    for _ in range(120):
        result = subprocess.run(["docker", "exec", NAMES[arm], "python", "-c", code],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        if not inspect(NAMES[arm])["State"]["Running"]:
            raise RuntimeError(f"{arm} exited during startup")
        time.sleep(3)
    raise RuntimeError(f"{arm} did not become ready")


def sync_results(path, prefix):
    if not prefix:
        return
    result = subprocess.run(["aws", "s3", "sync", str(path), prefix.rstrip("/") + "/",
                             "--only-show-errors", "--region", "us-east-1"])
    if result.returncode:
        raise RuntimeError("S3 evidence backup failed")


def report(run):
    command([sys.executable, ROOT / "run/write_provenance.py", run])
    with (run / "report.txt").open("w") as fh:
        result = subprocess.run([sys.executable, ROOT / "bench/report.py", run],
                                stdout=fh, stderr=subprocess.STDOUT,
                                env={**os.environ, "CENSUS_EMPTY_POLICY": "report"})
    return result.returncode


def main(args):
    lock = (ROOT / ".pdf1k-campaign.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run = args.output.resolve()
    if run.exists():
        raise FileExistsError(f"refusing to overwrite {run}")
    run.mkdir(parents=True)
    frozen = run / "frozen"
    frozen.mkdir()
    # Freeze the AWS pipeline, not an independently edited desktop copy.
    shutil.copy2(ROOT / "pipe/benchmark_pdf.pipe", frozen / "benchmark_pdf.pipe")
    shutil.copytree(ROOT / "arms/langgraph/tika-config", frozen / "tika-config")
    shutil.copytree(ROOT / "bench", frozen / "bench", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "metrics", frozen / "metrics", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "run", frozen / "run", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "arms/rocketride/data/probe", frozen / "probe")
    corpus = args.corpus.resolve()
    manifest = json.loads((corpus / "corpus_manifest.json").read_text())
    if manifest["n_measured"] != args.n or manifest["n_warm_extra"] != args.warm:
        raise ValueError("corpus manifest does not match N and disjoint warm-up")
    if set(manifest["docs_measured"]) & set(manifest["docs_warm"]):
        raise ValueError("warm-up overlaps measured corpus")
    if sorted(p.name for p in corpus.glob("*.pdf")) != manifest["docs_measured"] + manifest["docs_warm"]:
        raise ValueError("driver file order does not match the frozen corpus manifest")
    command(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=corpus)
    hf_cache = args.hf_cache.resolve()
    model = hf_cache / "hub/models--sentence-transformers--multi-qa-MiniLM-L6-cos-v1"
    revision = (model / "refs/main").read_text().strip()
    if not (model / "snapshots" / revision / "model.safetensors").is_file():
        raise ValueError("offline model checkpoint is missing")
    model_hashes = {str(p.relative_to(model)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(model.rglob("*")) if p.is_file()}
    save(frozen / "model-hashes.json", model_hashes)
    images = {k: inspect(v)["Id"] for k, v in {
        "rr": "bench-rocketride:official-87f530f",
        "lg": "bench-langgraph:profile-20261021",
        "tika": "apache/tika:3.2.3.0-full", "client": "bench-client:latest"}.items()}
    plan = {"n_docs": args.n, "warm_docs": args.warm, "mode": "b32",
            "baseline_reps": args.reps, "diagnostic_reps": 1,
            "profiler_safety_seconds": args.profile_seconds,
            "profile_scope": "entire measured workload, unless safety deadline reached",
            "arm_cpuset": ARM_CPUS, "client_cpuset": CLIENT_CPUS,
            "images": images, "corpus": str(corpus),
            "model_revision": revision, "shared_offline_model_cache": str(hf_cache),
            "pipeline_sha256": hashlib.sha256((frozen / "benchmark_pdf.pipe").read_bytes()).hexdigest(),
            "deadline_policy": "3600s outer request/batch; existing Tika 120s subcall retained and disclosed",
            "empty_policy": "report and compare named outcomes; does not certify no-text correctness",
            "profilers": {"rr": "engine Yappi 1.7.6 wall", "lg": "Yappi 1.7.6 wall"}}
    save(run / "plan.json", plan)
    for k in images:
        save(frozen / f"image-{k}.json", inspect(images[k]))
    (frozen / "pip-freeze-lg.txt").write_text(output(["docker", "run", "--rm", "--entrypoint", "pip", images["lg"], "freeze"]) + "\n")
    schedule = []
    for rep in range(1, args.reps + 1):
        for arm in (["rr", "lg"] if rep % 2 else ["lg", "rr"]):
            schedule.append(("baseline", rep, arm))
    schedule += [("diagnostic", 1, "lg"), ("diagnostic", 1, "rr")]
    plan["schedule"] = schedule
    save(run / "plan.json", plan)
    # Deliberately keep the opposite arm, including the public engine, idle-free.
    for name in ["bench-rocketride", "bench-langgraph", "bench-tika"]:
        subprocess.run(["docker", "stop", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if subprocess.run(["docker", "network", "inspect", NET], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        command(["docker", "network", "create", NET], stdout=subprocess.DEVNULL)
    for name in NAMES.values():
        if subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError(f"campaign container already exists: {name}")
    status = {"state": "running", "completed": [], "started_epoch": time.time()}
    try:
        for kind, rep, arm in schedule:
            cell = run / kind
            directory = cell / arm / f"rep{rep}"
            directory.mkdir(parents=True)
            status["current"] = {"kind": kind, "rep": rep, "arm": arm}
            save(run / "status.json", status)
            print(f"START {kind} {arm} rep{rep}", flush=True)
            command(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=corpus)
            services = ["tika", "lg"] if arm == "lg" else ["rr"]
            samplers = []
            try:
                for service in services:
                    run_service(service, images, kind == "diagnostic", frozen, hf_cache)
                version = ready(arm)
                if arm == "rr":
                    save(directory / "engine-version.json", json.loads(version))
                    (cell / "engine_sha.txt").write_text(output(["docker", "exec", NAMES[arm], "sha256sum", "/opt/rocketride/engine/engine"]) + "\n")
                else:
                    meta = output(["docker", "exec", NAMES[arm], "python", "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/meta').read().decode())"])
                    (cell / "meta_lg.json").write_text(meta + "\n")
                for service in services:
                    save(directory / f"container-{service}.json", inspect(NAMES[service]))
                    sampler = Sampler(NAMES[service], directory / ("sampler_tika.jsonl" if service == "tika" else "sampler.jsonl"))
                    sampler.start()
                    samplers.append(sampler)
                time.sleep(2)
                env = {"LG_URL": "http://pdf1k-langgraph:8100",
                    "ROCKETRIDE_URI": "ws://pdf1k-rocketride:5565/task/service",
                    "ROCKETRIDE_APIKEY": "local-dev", "BENCH_TIMEOUT_S": "3600",
                    "BENCH_PIPE": "/frozen/benchmark_pdf.pipe",
                    "BENCH_WARMUP_DOC": "/frozen/probe/sample.pdf",
                    "RR_PROFILE_SECONDS": str(args.profile_seconds if kind == "diagnostic" else 0),
                    "LG_PROFILE_SECONDS": str(args.profile_seconds if kind == "diagnostic" else 0)}
                driver = "rr_driver.py" if arm == "rr" else "lg_driver.py"
                cmd = ["docker", "run", "--rm", "--name", "pdf1k-client", "--network", NET,
                    "--cpuset-cpus", CLIENT_CPUS, "-v", f"{corpus}:/corpus:ro",
                    "-v", f"{directory}:/out", "-v", f"{frozen}:/frozen:ro"]
                cmd += env_args(env) + [images["client"], f"/frozen/bench/{driver}", "/corpus", "/out", str(args.n), "b32", str(args.warm)]
                with (directory / "driver.log").open("w") as fh:
                    command(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=args.cell_timeout)
                time.sleep(2)
            finally:
                for sampler in samplers:
                    sampler.stop()
                for service in services:
                    with (directory / f"{service}.log").open("w") as fh:
                        subprocess.run(["docker", "logs", NAMES[service]], stdout=fh, stderr=subprocess.STDOUT)
                    subprocess.run(["docker", "rm", "-f", NAMES[service]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.copy2(corpus / "SHA256SUMS", cell / "corpus.sha256")
            shutil.copy2(corpus / "corpus_manifest.json", cell / "corpus_manifest.json")
            shutil.copy2(frozen / "benchmark_pdf.pipe", cell / "pipeline.pipe")
            (cell / "image_ids.txt").write_text(f"rocketride {images['rr']} x86_64\nlanggraph {images['lg']} x86_64\ntika {images['tika']} x86_64\nclient {images['client']} x86_64\n")
            env_meta = {"stamp_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                "arch": "x86_64", "nproc": 32, "arm_cpus": 24, "mem_gb": 61,
                "os": output(["uname", "-s"]), "kernel": output(["uname", "-r"]),
                "git_sha": output(["git", "-C", ROOT, "rev-parse", "HEAD"]), "git_dirty": "true",
                "n_docs": args.n, "reps": args.reps if kind == "baseline" else 1,
                "mode": "b32", "lg_extractor": "tika", "warm_docs": args.warm,
                "arm_mem": "UNCAPPED", "timeout_s": 3600,
                "bench_cpuset": ARM_CPUS, "client_cpuset": CLIENT_CPUS,
                "rr_engine_version": "3.5.0.584", "rr_dup_patch": 0,
                "rr_engine_archive_sha256": "bfb0a7085fd93c59bc3c815ab28427a4dec2a49063d73d1a1c7e42bd5f3dd1e9",
                "rr_profile_seconds": env["RR_PROFILE_SECONDS"],
                "lg_profile_seconds": env["LG_PROFILE_SECONDS"], "census_empty_policy": "report"}
            (cell / "environment.txt").write_text("".join(f"{k}={v}\n" for k,v in env_meta.items()))
            status["completed"].append(status["current"])
            save(run / "status.json", status)
            sync_results(run, args.s3)
            print(f"DONE {kind} {arm} rep{rep}", flush=True)
        status["reports"] = {kind: report(run / kind)
                             for kind in dict.fromkeys(kind for kind, _, _ in schedule)}
        command([sys.executable, ROOT / "run/summarize_profile_campaign.py", run])
        status["state"] = "complete"
    except BaseException as exc:
        status["state"] = "failed"
        status["error"] = repr(exc)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", "pdf1k-client"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status["ended_epoch"] = time.time()
        save(run / "status.json", status)
        sync_results(run, args.s3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--hf-cache", required=True, type=Path)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--warm", type=int, default=24)
    parser.add_argument("--reps", type=int, default=0,
                        help="optional unprofiled repetitions per arm (default: 0)")
    parser.add_argument("--profile-seconds", type=int, default=7200)
    parser.add_argument("--cell-timeout", type=int, default=10800)
    parser.add_argument("--s3", default="")
    args = parser.parse_args()
    if args.reps < 0:
        parser.error("--reps must be nonnegative")
    main(args)
