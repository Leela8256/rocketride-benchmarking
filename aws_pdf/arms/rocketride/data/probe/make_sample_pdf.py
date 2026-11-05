"""Generate a deterministic, born-digital, text-rich probe PDF.

Written by hand (no reportlab) so the fixture is byte-reproducible on any
machine with stdlib Python and carries no personal or licensed content.
Regenerating always yields the same bytes -> the same SHA-256.

Usage:  python3 make_sample_pdf.py [out.pdf]
"""

import hashlib
import sys

PAGES = 6
LINES_PER_PAGE = 40
FONT_SIZE = 11
LEADING = 16
MARGIN_X = 62
TOP_Y = 742

# A fixed vocabulary cycled deterministically — no RNG, no locale dependence.
WORDS = (
    "pipeline document extraction benchmark vector embedding chunk latency "
    "throughput deterministic parser corpus token overlap splitter engine "
    "component lane transform ingestion retrieval semantic index harness "
    "measurement provenance reference canonical envelope contract adapter"
).split()


def line_text(page: int, line: int) -> str:
    """Deterministic ~62-char line, unique per (page, line)."""
    start = (page * LINES_PER_PAGE + line) * 7
    words = [WORDS[(start + i) % len(WORDS)] for i in range(8)]
    body = " ".join(words)
    return f"p{page + 1:02d}l{line + 1:02d} {body}"


def esc(s: str) -> bytes:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)").encode("ascii")


def build() -> bytes:
    objects: list[bytes] = []  # 1-indexed on write

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = 1
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pages_id = 2
    add(b"")  # placeholder, filled once page ids are known

    page_ids, content_ids = [], []
    for p in range(PAGES):
        lines = b"BT\n/F1 %d Tf\n%d TL\n%d %d Td\n" % (
            FONT_SIZE, LEADING, MARGIN_X, TOP_Y,
        )
        for ln in range(LINES_PER_PAGE):
            lines += b"(" + esc(line_text(p, ln)) + b") Tj T*\n"
        lines += b"ET"
        stream = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(lines), lines)
        cid = add(stream)
        content_ids.append(cid)
        pid = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, cid)
        )
        page_ids.append(pid)

    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
        len(page_ids), kids,
    )

    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)
    for i, body in enumerate(objects, start=1):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"

    xref_at = len(out)
    n = len(objects) + 1
    out += b"xref\n0 %d\n" % n
    out += b"0000000000 65535 f \n"
    for i in range(1, n):
        out += b"%010d 00000 n \n" % offsets[i]
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        n, catalog_id, xref_at,
    )
    return bytes(out)


def expected_text() -> str:
    """The text a faithful extractor should recover, in reading order."""
    return "\n".join(
        line_text(p, ln) for p in range(PAGES) for ln in range(LINES_PER_PAGE)
    )


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "sample.pdf"
    data = build()
    with open(path, "wb") as fh:
        fh.write(data)
    txt = expected_text()
    print(f"wrote {path}: {len(data)} bytes")
    print(f"pdf  sha256: {hashlib.sha256(data).hexdigest()}")
    print(f"pages: {PAGES}  lines/page: {LINES_PER_PAGE}")
    print(f"expected extractable chars: {len(txt)} (newline-joined lines)")
    print(f"expected-text sha256: {hashlib.sha256(txt.encode()).hexdigest()}")
