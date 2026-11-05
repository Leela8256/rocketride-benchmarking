"""M0 — the correctness gate. See README for the logic of each check.

Doctrine: every check FAILS CLOSED. A missing field, an unproven state, or
a pending sub-check is a violation, never a pass — absence of evidence is
absence of correctness. The one sanctioned escape hatch is `expected_empty`:
a named set of known no-text docs allowed to produce zero content, either as
a zero-chunk completion or as an explicit no_documents failure (000164.pdf
does the former on LG and the latter on RR). Any other doc that produces
nothing is a defect.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from metrics.records import ok_records

NORM_TOL = 1e-3
EMBED_DIM = 384

# Reasons under which a doc in expected_empty may legitimately fail: the
# parser found no text and said so explicitly. Anything else (timeout,
# transport, proof-missing) is a real failure even for an allowlisted doc.
EMPTY_FAIL_REASONS = frozenset({"no_documents"})

# Per-arm record contract: fields that must be PRESENT and exactly True on
# every completed record. RR proves vector finiteness upstream (the driver
# folds it into vector_dim, so a non-finite vector can never reach a
# vector_dim of 384); LG emits explicit flags, so their absence is a defect.
REQUIRED_TRUE = {
    "rr": ("identity_ok",),
    "lg": ("identity_ok", "sha_header_ok", "vectors_finite"),
}

MAX_ERROR_EXAMPLES = 5


def failure_bucket(r: Dict) -> str:
    """Stable failure-mode key: driver reason, else the exception type.

    Never a raw message prefix — two distinct errors sharing 30 leading
    chars must not collapse into one bucket. Full messages are preserved
    per bucket in census()'s failure_examples.
    """
    if r.get("reason"):
        return r["reason"]
    err = r.get("error") or r.get("error_raw")
    if err:
        head = str(err).split(":", 1)[0].strip()
        return (head or "unknown")[:80]
    return "unknown"


def census(rows: List[Dict], offered: int,
           expected_docs: Optional[Set[str]] = None,
           expected_empty: Set[str] = frozenset(),
           empty_policy: str = "fail") -> Dict[str, Any]:
    """Loss detection. Every offered doc must come back as exactly one
    record, and every failure must be explicit and expected. With
    expected_docs (the corpus manifest) silent drops are named, not just
    counted.

    empty_policy governs ONLY explicit empty-extraction outcomes (a parser
    that handled the file and found no text):

      "fail"   -- the default. Only docs NAMED in expected_empty may come
                  back empty; anything else is a defect.
      "report" -- any empty extraction is named and counted but does not
                  fail the gate. For heterogeneous corpora where scanned,
                  text-free PDFs are a property of the input.

    The hatch never widens beyond that. LOST, DUPLICATE, MISSING and
    UNEXPECTED docs remain fatal under either policy, as do timeouts and
    transport errors — including on a doc named in expected_empty.
    """
    if empty_policy not in ("fail", "report"):
        raise ValueError(f"empty_policy must be 'fail' or 'report', got {empty_policy!r}")
    docs = [r["doc"] for r in rows]
    counts = Counter(docs)
    duplicates = sorted(d for d, n in counts.items() if n > 1)
    ok = ok_records(rows)
    fails = [r for r in rows if not r.get("ok")]

    by_reason: Dict[str, List[str]] = {}
    examples: Dict[str, List[str]] = {}
    for r in fails:
        bucket = failure_bucket(r)
        by_reason.setdefault(bucket, []).append(r["doc"])
        msg = str(r.get("error") or r.get("error_raw") or "")[:300]
        if msg and msg not in examples.setdefault(bucket, []):
            if len(examples[bucket]) < MAX_ERROR_EXAMPLES:
                examples[bucket].append(msg)

    # Docs the parser handled but found no text in. Under "report" every one
    # of them is tolerated; under "fail" only the named allowlist is.
    empty_docs = sorted(d for reason, ds in by_reason.items()
                        if reason in EMPTY_FAIL_REASONS for d in ds)
    tolerated = set(empty_docs) if empty_policy == "report" else set(expected_empty)
    unexpected = [d for reason, ds in by_reason.items() for d in ds
                  if not (reason in EMPTY_FAIL_REASONS and d in tolerated)]

    missing = unexpected_docs = None
    if expected_docs is not None:
        seen = set(docs)
        missing = sorted(set(expected_docs) - seen)
        unexpected_docs = sorted(seen - set(expected_docs))

    return {
        "offered": offered,
        "records": len(rows),
        "silent": offered - len(rows),
        "duplicate_docs": duplicates,
        "missing_docs": missing,          # None = no manifest supplied
        "unexpected_docs": unexpected_docs,
        "completed": len(ok),
        "failed_by_reason": {k: len(v) for k, v in by_reason.items()},
        "failed_docs": by_reason,
        "failure_examples": examples,
        "unexpected_failures": len(unexpected),
        "unexpected_failure_docs": sorted(unexpected),
        # Named regardless of policy: under "report" these are the docs the
        # gate tolerated, and a reader must be able to see exactly which.
        "empty_policy": empty_policy,
        "empty_extraction_count": len(empty_docs),
        "empty_extraction_docs": empty_docs,
        "PASS": (len(rows) == offered and not duplicates and not unexpected
                 and not missing and not unexpected_docs),
    }


def structure(rows: List[Dict], arm: str,
              expected_empty: Set[str] = frozenset()) -> Dict[str, Any]:
    """Corruption detection on completed docs. Fail-closed per-arm field
    contract; zero-chunk completions are violations unless the doc is
    allowlisted in expected_empty (and even then identity must hold)."""
    if arm not in REQUIRED_TRUE:
        raise ValueError(f"unknown arm {arm!r}: no record contract defined")
    required = REQUIRED_TRUE[arm]
    bad: Dict[str, List[str]] = {}
    empty: List[str] = []
    for r in ok_records(rows):
        doc = r["doc"]
        violations = [f"{f}={r.get(f)!r}" for f in required
                      if r.get(f) is not True]
        n = r.get("n_chunks")
        if n == 0:
            if doc not in expected_empty:
                violations.append("empty_not_allowlisted")
            if violations:
                bad[doc] = violations
            else:
                empty.append(doc)
            continue
        if not isinstance(n, int) or n < 1:
            violations.append(f"n_chunks={n!r}")
        if r.get("vector_dim") != EMBED_DIM:
            violations.append(f"vector_dim={r.get('vector_dim')!r}")
        norms = r.get("l2_norms_minmax")
        if (not isinstance(norms, (list, tuple)) or len(norms) != 2
                or not all(isinstance(x, (int, float)) for x in norms)
                or abs(norms[0] - 1) >= NORM_TOL
                or abs(norms[1] - 1) >= NORM_TOL):
            violations.append(f"l2_norms={norms!r}")
        hashes = r.get("chunk_sha256")
        if not isinstance(hashes, list) or (isinstance(n, int) and len(hashes) != n):
            violations.append(
                f"chunk_hashes={'missing' if hashes is None else len(hashes)}/{n}")
        if violations:
            bad[doc] = violations
    return {"bad_docs": bad, "completed_empty": empty, "PASS": not bad}


def determinism(rows_a: List[Dict], rows_b: List[Dict]) -> Dict[str, Any]:
    """Instability detection, run-over-run (or mode-over-mode). Ordered
    chunk hashes must match per doc, outcomes must not flip between runs,
    and hashes must actually exist — None on both sides is unproven, not
    equal."""
    a = {r["doc"]: r.get("chunk_sha256") for r in ok_records(rows_a)}
    b = {r["doc"]: r.get("chunk_sha256") for r in ok_records(rows_b)}
    both = sorted(set(a) & set(b))
    mismatch = [d for d in both
                if a[d] is None or b[d] is None or a[d] != b[d]]
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    return {"compared": len(both), "mismatch_docs": mismatch,
            "only_in_a": only_a, "only_in_b": only_b,
            "PASS": bool(both) and not mismatch and not only_a and not only_b}


def ground_truth_match(rows: List[Dict], gt: Dict[str, Dict],
                       check_offsets: bool = False) -> Dict[str, Any]:
    """Byte tier: chunk hashes (and optionally offsets) vs a reference.
    Zero coverage is a vacuous result, not a pass."""
    covered = mism = 0
    mismatch_docs = []
    for r in ok_records(rows):
        g = gt.get(r["doc"])
        if not g:
            continue
        covered += 1
        rh, gh = r.get("chunk_sha256"), g.get("chunk_sha256")
        bad = rh is None or gh is None or rh != gh
        if check_offsets and not bad:
            bad = r.get("offsets") != g.get("offsets")
        if bad:
            mism += 1
            mismatch_docs.append(r["doc"])
    return {"covered": covered, "mismatches": mism,
            "mismatch_docs": mismatch_docs[:15],
            "PASS": covered > 0 and mism == 0}


def cross_arm(rows_a: List[Dict], rows_b: List[Dict],
              arm_a: str = "lg", arm_b: str = "rr",
              hard_ratio: Tuple[float, float] = (0.4, 2.5),
              warn_ratio: Tuple[float, float] = (0.8, 1.25),
              require_byte_parity: bool = False) -> Dict[str, Any]:
    """Cross-arm equivalence: did the two frameworks do the SAME WORK?

    Every other check in this module validates one arm against its own
    contract. Two arms can each be internally perfect -- 384 dims, finite,
    deterministic, correctly identified -- while processing different amounts
    of text, and then a throughput comparison between them is meaningless.
    Measured on this project: median chunk delta 0 but max +89 chunks and a
    char ratio up to 1.977, i.e. one arm nearly doubled the other's work on at
    least one document.

    Two bands, from metrics/README:
      hard  (default 0.4-2.5)  -- outside this a whole document was dropped or
                                  duplicated; not explainable by parser
                                  differences. FAILS the run.
      warn  (default 0.8-1.25) -- real workload asymmetry. Reported, and it is
                                  why chunks_per_s must be published beside
                                  docs_per_s. Does NOT fail.

    `require_byte_parity` is for matched-extractor runs (both arms on Tika),
    where ordered chunk hashes are expected to be identical. Byte parity is
    always MEASURED and reported; this only decides whether it GATES.

    Only documents that succeeded on BOTH arms are compared -- a doc that
    failed one side is already a census/structure failure there and must not
    be double-counted as a parity failure here. Zero comparable documents is
    vacuous, and vacuous is a FAIL, not a pass.
    """
    hard_lo, hard_hi = hard_ratio
    warn_lo, warn_hi = warn_ratio
    a = {r["doc"]: r for r in ok_records(rows_a)}
    b = {r["doc"]: r for r in ok_records(rows_b)}
    both = sorted(set(a) & set(b))

    identical, differing, no_hashes = [], [], []
    ratios: Dict[str, float] = {}
    hard_bad, warn_bad = [], []
    for d in both:
        ha, hb = a[d].get("chunk_sha256"), b[d].get("chunk_sha256")
        if ha is None or hb is None:
            no_hashes.append(d)          # unproven, never counted as equal
        elif ha == hb:
            identical.append(d)
        else:
            differing.append(d)
        na, nb = a[d].get("n_chunks"), b[d].get("n_chunks")
        if isinstance(na, int) and isinstance(nb, int) and na > 0:
            r = nb / na
            ratios[d] = round(r, 4)
            if not (hard_lo <= r <= hard_hi):
                hard_bad.append(d)
            elif not (warn_lo <= r <= warn_hi):
                warn_bad.append(d)

    vals = sorted(ratios.values())
    stats = {
        "median": vals[len(vals) // 2] if vals else None,
        "min": vals[0] if vals else None,
        "max": vals[-1] if vals else None,
        "n": len(vals),
    }
    byte_parity = bool(both) and not differing and not no_hashes
    passed = (
        bool(both)
        and not hard_bad
        and not no_hashes
        and (byte_parity if require_byte_parity else True)
    )
    return {
        "compared": len(both),
        "only_in_a": sorted(set(a) - set(b)),
        "only_in_b": sorted(set(b) - set(a)),
        "byte_identical": len(identical),
        "byte_differing": len(differing),
        "differing_docs": differing[:15],
        "missing_hashes": no_hashes[:15],
        "byte_parity": byte_parity,
        "byte_parity_gated": require_byte_parity,
        f"chunk_ratio_{arm_b}_over_{arm_a}": stats,
        "hard_band": [hard_lo, hard_hi],
        "hard_violations": hard_bad,
        "warn_band": [warn_lo, warn_hi],
        "warn_violations": warn_bad,
        "PASS": passed,
    }


def input_integrity(rows: List[Dict],
                    manifest_sha: Dict[str, str]) -> Dict[str, Any]:
    """Every record's input_sha256 must equal the canonical corpus manifest.

    Records carry the hash of the bytes the driver actually read. Nothing
    previously compared it to anything, so a corpus that drifted between arms
    or between reps -- a stale file, a partial copy, a re-fetch that picked
    different documents -- would produce a clean-looking run over different
    inputs. Because both arms are checked against the SAME manifest, matching
    it also proves the two arms read identical bytes.

    A record with no input_sha256 is UNVERIFIED, which fails: absence of
    evidence is absence of correctness.
    """
    mismatched, unverified, unknown = [], [], []
    for r in rows:
        doc, got = r["doc"], r.get("input_sha256")
        want = manifest_sha.get(doc)
        if want is None:
            unknown.append(doc)
        elif not got:
            unverified.append(doc)
        elif got != want:
            mismatched.append(doc)
    return {
        "checked": len(rows),
        "manifest_docs": len(manifest_sha),
        "mismatched_docs": mismatched[:15],
        "unverified_docs": unverified[:15],
        "not_in_manifest": unknown[:15],
        "PASS": bool(rows) and bool(manifest_sha)
                and not mismatched and not unverified and not unknown,
    }


def self_duplication(rows: List[Dict]) -> Dict[str, Any]:
    """Does an arm emit the same document list more than once?

    A node that flushes its batch downstream and then also falls through to
    the default forwarding action returns [A,B,C,A,B,C]. The chunk hashes are
    valid, the vectors are valid, every per-document check passes -- and the
    arm has silently done twice the work. Cross-arm parity would catch it only
    if the other arm is present and correct; this catches it in ONE arm, which
    is what makes it a usable permanent gate.

    repeat_factor is the smallest k > 1 for which the ordered hash list equals
    its first n/k elements repeated k times; 1 means no repetition. Measured on
    RocketRide 3.3.1 before the BUG_CHUNK_DUPLICATION patch: 51 of 987
    documents at factor 2, LangGraph 0 of 987.
    """
    def factor(h: List[str]) -> int:
        n = len(h)
        if n < 2:
            return 1
        for k in range(2, n + 1):
            if n % k:
                continue
            p = n // k
            if all(h[i] == h[i % p] for i in range(n)):
                return k
        return 1

    dup: Dict[str, int] = {}
    for r in ok_records(rows):
        h = r.get("chunk_sha256")
        if isinstance(h, list) and len(h) > 1:
            k = factor(h)
            if k > 1:
                dup[r["doc"]] = k
    return {
        "checked": len(ok_records(rows)),
        "duplicated_docs": len(dup),
        "factors": sorted(set(dup.values())),
        "examples": sorted(dup)[:15],
        "PASS": not dup,
    }


def parity_fixture(vec_a: Optional[List[float]], vec_b: Optional[List[float]],
                   atol: float = 1e-5) -> Dict[str, Any]:
    if not (isinstance(vec_a, list) and isinstance(vec_b, list)
            and len(vec_a) == len(vec_b) and vec_a):
        return {"PASS": False, "error": "vector missing or length mismatch"}
    worst = max(abs(x - y) for x, y in zip(vec_a, vec_b))
    return {"max_abs_diff": worst, "atol": atol, "PASS": worst < atol}


def gate_verdict(*checks: Optional[Dict[str, Any]]) -> bool:
    """True only when every check has PASS exactly True. None, a missing
    check, or any placeholder ('PENDING', a truthy dict) fails closed."""
    return all(isinstance(c, dict) and c.get("PASS") is True for c in checks)
