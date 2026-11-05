"""Typed settings from env + runtime thread-pinning.

configure_runtime() must run before any heavy import (torch, tokenizers):
the *_NUM_THREADS env pins only take effect if set before those libraries
initialize.
"""

import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional


class SettingsError(ValueError):
    pass


def _to_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from exc


def _to_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"{key} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    executor_workers: int = 4
    max_inflight_requests: int = 8
    request_timeout_seconds: int = 300
    torch_threads: int = 1
    torch_interop_threads: int = 1
    max_upload_bytes: int = 104_857_600  # 100 MiB — judgment call, see toil.md
    temp_directory: str = field(default_factory=tempfile.gettempdir)
    pipelines: List[str] = field(default_factory=lambda: ["document-v1"])
    host: str = "0.0.0.0"
    port: int = 8100
    log_level: str = "info"
    benchmark_mode: bool = False
    uvicorn_workers: int = 1  # fixed deployment shape

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        if env is None:
            env = os.environ
        pipelines_raw = env.get("PIPELINES", "document-v1")
        pipelines = [p.strip() for p in pipelines_raw.split(",") if p.strip()]
        if not pipelines:
            raise SettingsError("PIPELINES must name at least one pipeline")
        return cls(
            executor_workers=_to_int(env, "EXECUTOR_WORKERS", 4),
            max_inflight_requests=_to_int(env, "MAX_INFLIGHT_REQUESTS", 8),
            request_timeout_seconds=_to_int(env, "REQUEST_TIMEOUT_SECONDS", 300),
            torch_threads=_to_int(env, "TORCH_THREADS", 1),
            torch_interop_threads=_to_int(env, "TORCH_INTEROP_THREADS", 1),
            max_upload_bytes=_to_int(env, "MAX_UPLOAD_BYTES", 104_857_600),
            temp_directory=env.get("TEMP_DIRECTORY", tempfile.gettempdir()),
            pipelines=pipelines,
            host=env.get("HOST", "0.0.0.0"),
            port=_to_int(env, "PORT", 8100),
            log_level=env.get("LOG_LEVEL", "info"),
            benchmark_mode=_to_bool(env, "BENCHMARK_MODE", False),
        )


def configure_runtime(settings: Settings) -> Dict[str, str]:
    """Pin BLAS/tokenizer thread env vars. Returns what was applied."""
    pins = {
        "OMP_NUM_THREADS": str(settings.torch_threads),
        "MKL_NUM_THREADS": str(settings.torch_threads),
        "OPENBLAS_NUM_THREADS": str(settings.torch_threads),
        "TOKENIZERS_PARALLELISM": "false",
    }
    for key, value in pins.items():
        os.environ[key] = value
    return pins
