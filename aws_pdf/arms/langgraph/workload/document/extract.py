"""Extractor selection — the EXTRACTOR switch.

EXTRACTOR=pypdf  (default) -> extract_pdf.py, the pinned pypdf 6.15.0 path.
EXTRACTOR=tika             -> Apache Tika server sidecar (pin 3.2.3 — the
                              version RocketRide bundles) at TIKA_URL,
                              configured with RocketRide's own
                              tika-config.xml so parser settings match
                              (sortByPosition, form/annotation/bookmark
                              extraction, OCR excluded).

Pure computation layer: stdlib HTTP only, no fastapi/langgraph imports.
The mode is read once at import; changing it requires a service restart,
which keeps any single run single-mode by construction.
"""

import os
import urllib.request

EXTRACTOR = os.environ.get("EXTRACTOR", "pypdf").strip().lower()
TIKA_URL = os.environ.get("TIKA_URL", "http://tika:9998")


def extract_tika(source_path: str) -> str:
    """PDF -> text via the Tika server sidecar (PUT /tika, text/plain)."""
    with open(source_path, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(
        f"{TIKA_URL}/tika",
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/pdf",
            "Accept": "text/plain; charset=UTF-8",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    # Tika pads output with blank lines front/back; normalize the frame only,
    # never interior whitespace (chunk hashes depend on it).
    return text.strip("\n")


def extract(source_path: str) -> str:
    if EXTRACTOR == "tika":
        return extract_tika(source_path)
    from workload.document.extract_pdf import extract_pdf

    return extract_pdf(source_path)


def extractor_info() -> dict:
    info = {"mode": EXTRACTOR}
    if EXTRACTOR == "tika":
        info["tika_url"] = TIKA_URL
        info["tika_pinned"] = "3.2.3 (RocketRide-bundled version, RR tika-config.xml)"
    else:
        from workload.document.extract_pdf import pypdf_version

        info["pypdf"] = pypdf_version()
    return info
