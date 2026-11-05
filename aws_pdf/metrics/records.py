"""Shared loading for per-doc JSONL run records."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

META_KINDS = {"shot_meta", "level_meta"}


def load_records(path) -> Tuple[List[Dict[str, Any]], Optional[Dict], List[Dict]]:
    """Return (doc_records, meta, events) from a per_doc.jsonl file."""
    rows, meta, events = [], None, []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        kind = r.get("kind")
        if kind in META_KINDS:
            meta = r
        elif kind:
            events.append(r)
        else:
            rows.append(r)
    return rows, meta, events


def ok_records(rows: List[Dict]) -> List[Dict]:
    return [r for r in rows if r.get("ok")]


def latency_s(r: Dict) -> float:
    return (r["completion_ns"] - r["submit_ns"]) / 1e9


def by_completion(rows: List[Dict]) -> List[Dict]:
    return sorted((r for r in rows if "completion_ns" in r),
                  key=lambda r: r["completion_ns"])
