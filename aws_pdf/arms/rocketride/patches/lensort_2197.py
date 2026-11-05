#!/usr/bin/env python3
"""Apply rocketride-server PR #2197 to an extracted engine tree.

fix(embedding): batch sentences by length so padding stops wasting compute.
_encode_local batched in arrival order while tokenization pads each batch to its
longest member, so one long sentence made every other pay its cost. Sort by
length, batch, then restore the caller's order.

Idempotent: prints ALREADY PATCHED and exits 0 if the marker is present.
Usage: lensort_2197.py <engine_dir>
"""
import sys
from pathlib import Path

MARKER = "order = sorted(range(len(sentences))"

OLD_LOOP = """        # Process in batches
        for i in range(0, len(sentences), batch_size):
            batch = sentences[i : i + batch_size]
"""

NEW_LOOP = """        # Batch sentences of similar length: each batch is padded to its longest
        # member, so a long one makes every other pay its cost. On CPU with mixed
        # lengths this is worth +30% at 64 sentences per call and +87% at 1024,
        # and nothing when they are all one length. Order is restored below.
        order = sorted(range(len(sentences)), key=lambda i: len(sentences[i]))

        # Process in batches
        for i in range(0, len(order), batch_size):
            indexes = order[i : i + batch_size]
            batch = [sentences[index] for index in indexes]
"""

OLD_RET = """        return np.array(all_embeddings)
"""

NEW_RET = """        # Undo the length sort so embeddings line up with the input sentences
        restored = [None] * len(all_embeddings)
        for position, index in enumerate(order):
            restored[index] = all_embeddings[position]

        return np.array(restored)
"""


def main(engine_dir: str) -> int:
    hits = list(Path(engine_dir).rglob("ai/common/models/transformers/sentence_transformers.py"))
    if not hits:
        print("PATCH 2197 FAILED: sentence_transformers.py not found", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"PATCH 2197 FAILED: {len(hits)} candidates: {hits}", file=sys.stderr)
        return 1

    f = hits[0]
    src = f.read_text()
    if MARKER in src:
        print(f"PATCH 2197: ALREADY PATCHED ({f})")
        return 0

    for name, old in (("batch loop", OLD_LOOP), ("return", OLD_RET)):
        n = src.count(old)
        if n != 1:
            print(f"PATCH 2197 FAILED: {name} anchor found {n} times (need 1)", file=sys.stderr)
            return 1

    src = src.replace(OLD_LOOP, NEW_LOOP).replace(OLD_RET, NEW_RET)
    f.write_text(src)

    check = f.read_text()
    if MARKER not in check or "restored[index] = all_embeddings[position]" not in check:
        print("PATCH 2197 FAILED: verification after write", file=sys.stderr)
        return 1
    compile(check, str(f), "exec")          # syntax must still be valid
    print(f"PATCH 2197: APPLIED to {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/opt/rocketride"))
