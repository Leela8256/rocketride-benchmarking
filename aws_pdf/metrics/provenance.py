"""Per-run provenance — what makes a number traceable to what produced it.

A performance number without its environment is not a result, it is a rumour.
Every field below must be RECORDED, and fields that could be assumed are
instead read from the running system (architecture, cpu count, image digest),
because config intent and runtime reality diverge -- this suite has already
been bitten by /meta reporting a configured concurrency the code never used.

`missing()` is the enforcement: a run whose provenance is incomplete is not
publishable, regardless of how good the numbers look.
"""

from typing import Any, Dict, List

# Every one of these must be present and non-null for a run to be quotable.
REQUIRED = (
    "run_id", "timestamp_utc",
    "git_commit", "image_digest", "framework_version",
    "instance_type", "architecture", "cpu_count", "ram_gb",
    "corpus_manifest_sha256", "corpus_n_docs",
    "parser", "parser_config_hash", "chunk_config",
    "rocketride_engine_version", "duplication_patch_applied",
    "duplication_patch_id", "rocketride_sdk_version",
    "embedding_model",
    "offered_concurrency", "configured_concurrency",
    "warmup_policy", "timeout_s",
    "mode",
)


def missing(record: Dict[str, Any]) -> List[str]:
    return [k for k in REQUIRED
            if k not in record or record[k] is None or record[k] == ""]


def check(record: Dict[str, Any]) -> Dict[str, Any]:
    gaps = missing(record)
    return {
        "PASS": not gaps,
        "missing_fields": gaps,
        "note": ("complete" if not gaps else
                 "INCOMPLETE PROVENANCE — run is not publishable"),
    }
