"""Build a fault corpus: N clean documents with poison injected at known positions.

Every poison file carries a VALID %PDF header where it can. That is the whole
point: a file rejected at upload validation tests the web server, not the
pipeline, and a wrong magic byte lets a framework skip the parser entirely.
These must fail INSIDE the parser, which is where isolation actually matters.

Ordering is by filename, because every driver does sorted(glob("*.pdf")). The
poison files are named to land at exactly the requested positions.

  python3 make_faults.py <clean_corpus> <dest> <n_clean> <pos,pos,...>
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# Four parser-level failure modes, all constructible with the stdlib (the box
# has no pip). Each fails for a different reason, so a framework that handles
# one may still mishandle another.
KINDS = ("corrupt", "zero_byte", "truncated", "oversized_garbage")


def make_poison(kind: str, donor: Path) -> bytes:
    if kind == "corrupt":
        # Valid header, then noise. Defeats magic-byte sniffing; dies in the
        # object parser.
        return b"%PDF-1.7\n" + os.urandom(64 * 1024)
    if kind == "zero_byte":
        return b""
    if kind == "truncated":
        # A real PDF cut mid-object: header and some objects valid, xref gone.
        raw = donor.read_bytes()
        return raw[: max(1024, len(raw) // 3)]
    if kind == "oversized_garbage":
        return b"%PDF-1.7\n" + os.urandom(5 * 1024 * 1024)
    raise ValueError(kind)


def main():
    clean_dir, dest = Path(sys.argv[1]), Path(sys.argv[2])
    n_clean = int(sys.argv[3])
    positions = [int(x) for x in sys.argv[4].split(",")]
    assert len(positions) == len(KINDS), f"{len(KINDS)} kinds, {len(positions)} positions"

    clean = sorted(clean_dir.glob("*.pdf"))[:n_clean]
    assert len(clean) == n_clean, f"only {len(clean)} clean docs in {clean_dir}"

    dest.mkdir(parents=True, exist_ok=True)
    for f in dest.glob("*.pdf"):
        f.unlink()

    # Interleave: build the final ordered list, then name files so that
    # sorted() reproduces exactly that order. zzz-prefixing would sort poison
    # to the end and defeat the positional design.
    order = list(clean)
    for pos, kind in sorted(zip(positions, KINDS)):
        order.insert(pos, ("POISON", kind))

    faults, manifest_docs = [], []
    width = len(str(len(order)))
    for idx, item in enumerate(order):
        if isinstance(item, tuple):
            kind = item[1]
            name = f"{idx:0{width}d}_fault_{kind}.pdf"
            data = make_poison(kind, clean[0])
            faults.append({"doc": name, "type": f"fault_{kind}",
                           "position": idx, "bytes": len(data)})
        else:
            name = f"{idx:0{width}d}_{item.name}"
            data = item.read_bytes()
        (dest / name).write_bytes(data)
        manifest_docs.append({"doc": name, "sha256": hashlib.sha256(data).hexdigest(),
                              "source": None if isinstance(item, tuple) else item.name})

    (dest / "fault_manifest.json").write_text(json.dumps({
        "faults": faults,
        "n_clean": n_clean,
        "n_total": len(order),
        "requested_positions": positions,
        # The clean docs keep a pointer to their ORIGINAL filename so their
        # output can be checked against a known-good baseline from a prior run.
        "docs": manifest_docs,
    }, indent=1))

    import subprocess
    with open(dest / "SHA256SUMS", "w") as fh:
        subprocess.run(["sha256sum"] + sorted(p.name for p in dest.glob("*.pdf")),
                       cwd=dest, stdout=fh, check=True)
    print(f"fault corpus: {len(order)} files ({n_clean} clean + {len(faults)} poison)")
    for f in faults:
        print(f"  position {f['position']:>4}  {f['doc']}  {f['bytes']} bytes")


if __name__ == "__main__":
    main()
