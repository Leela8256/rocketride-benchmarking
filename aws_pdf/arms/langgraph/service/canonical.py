"""Canonical JSON encoder — the single source of response bytes.

⚠️  OPEN-1 — PROVISIONAL VALUES. These flags MUST byte-match the encoder
that produced the offline mt10k reference. That verification happens in M3
(parity: offline reference == workload == graph == HTTP). If the reference
differs, FLIP THESE CONSTANTS to match it — do not adapt the reference.
Until M3 confirms them, no parity number computed against this encoder can
be trusted.
"""

import hashlib
import json

SEPARATORS = (",", ":")
ENSURE_ASCII = True
SORT_KEYS = False
ALLOW_NAN = False


class CanonicalEncodingError(Exception):
    """Raised when a value cannot be canonically encoded (NaN/Inf/non-serializable)."""


def canonical_encode(obj: object) -> bytes:
    """Encode obj to canonical UTF-8 JSON bytes. Never emits NaN/Inf."""
    try:
        text = json.dumps(
            obj,
            separators=SEPARATORS,
            ensure_ascii=ENSURE_ASCII,
            sort_keys=SORT_KEYS,
            allow_nan=ALLOW_NAN,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalEncodingError(str(exc)) from exc
    return text.encode("utf-8")


def canonical_sha256(data: bytes) -> str:
    """Hex sha256 of canonical bytes — what X-Output-SHA256 carries."""
    return hashlib.sha256(data).hexdigest()
