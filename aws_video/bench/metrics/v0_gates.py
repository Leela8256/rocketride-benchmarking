"""V0 — validity gates. Fail-closed: a missing field or unproven state is a
violation, never a default pass. A run failing any HARD gate produces no
quotable numbers (METRICS.md).

Every gate returns {"gate", "status": PASS|FAIL|WARN|SKIP, "detail"}.
SKIP means the input lacks the field the gate needs (e.g. old records
without n_frames_est) — the report surfaces it loudly; it is not a pass.
"""

from collections import Counter

EMBED_DIM = 384
NORM_TOL = 1e-3
FRAME_INTERVAL_S = 15
CHUNK_MAX_CHARS = 4096
# Cross-arm bands (functional equivalence — byte parity deliberately not
# required, decision 2026-08-20):
DET_RATIO_WARN = (0.90, 1.10)
CHUNK_RATIO_HARD = (0.4, 2.5)
CHUNK_RATIO_WARN = (0.8, 1.25)


def _g(gate, status, detail):
    return {"gate": gate, "status": status, "detail": detail}


def census(records, manifest_docs):
    """Loss: offered == returned, attributed, unique, no unexpected failures."""
    names = [r["doc"] for r in records]
    problems = []
    if len(names) != len(set(names)):
        problems.append("duplicate records: "
                        + str([d for d, c in Counter(names).items() if c > 1][:5]))
    missing = sorted(set(manifest_docs) - set(names))
    extra = sorted(set(names) - set(manifest_docs))
    if missing:
        problems.append(f"missing from records: {missing[:5]}{'…' if len(missing) > 5 else ''}")
    if extra:
        problems.append(f"not in manifest: {extra[:5]}")
    bad_id = [r["doc"] for r in records if not r.get("identity_ok")]
    if bad_id:
        problems.append(f"identity_ok false: {bad_id[:5]}")
    failed = [(r["doc"], r.get("reason")) for r in records if not r.get("ok")]
    if failed:
        problems.append(f"failed docs: {failed[:5]}{'…' if len(failed) > 5 else ''}")
    return _g("census", "FAIL" if problems else "PASS",
              "; ".join(problems) or f"{len(records)}/{len(manifest_docs)} docs, all ok")


def structure(records):
    """Corruption: chunks exist, vectors are 384-dim/finite/normalized,
    hash count matches chunk count, chunk sizes inside the splitter window."""
    problems = []
    for r in records:
        if not r.get("ok"):
            continue                      # census already names failures
        d = r["doc"]
        if (r.get("n_chunks") or 0) < 1:
            problems.append(f"{d}: no chunks")
        if r.get("vector_dim") != EMBED_DIM:
            problems.append(f"{d}: vector_dim={r.get('vector_dim')}")
        if len(r.get("chunk_sha256") or []) != r.get("n_chunks"):
            problems.append(f"{d}: hash count != chunk count")
        mm = r.get("l2_norms_minmax")
        if not mm or abs(mm[0] - 1) > NORM_TOL or abs(mm[1] - 1) > NORM_TOL:
            problems.append(f"{d}: norms {mm}")
        n, chars = r.get("n_chunks") or 0, r.get("total_chars") or 0
        if n and chars / n > CHUNK_MAX_CHARS:
            problems.append(f"{d}: mean chunk {chars / n:.0f} chars > {CHUNK_MAX_CHARS}")
    return _g("structure", "FAIL" if problems else "PASS",
              "; ".join(problems[:6]) or "384-dim finite normalized, counts consistent")


def frame_law(records):
    """Silent frame drops: frames == floor(duration/15)+1 (±1) per video."""
    checked, problems, missing, oversampled = 0, [], 0, []
    for r in records:
        if not r.get("ok"):
            continue
        dur = r.get("video_duration_s") or r.get("duration_s")
        if r.get("n_frames_est") is None or not dur:
            missing += 1
            continue
        checked += 1
        expect = int(dur // FRAME_INTERVAL_S) + 1
        delta = r["n_frames_est"] - expect
        # The gate exists to catch silent frame DROPS. Under-extraction
        # beyond +/-1 is a hard problem; OVER-extraction up to 10% is the
        # measured VFR/odd-timestamp behavior of old prints (2026-08-22)
        # and warns instead.
        if delta < -1:
            problems.append(f"{r['doc']}: {r['n_frames_est']} frames, expected ~{expect} (UNDER)")
        elif delta > 1 and delta / expect > 0.10:
            problems.append(f"{r['doc']}: {r['n_frames_est']} frames, expected ~{expect} (over >10%)")
        elif delta > 1:
            oversampled.append(f"{r['doc']}: +{delta}")
        # detect-pipe bound (haystack-suite interval probe, recalibrated
        # 2026-08-22): chunks track text volume and exceed frames in dense
        # rooms — measured up to 1.84x on IN-series (16+ detections/frame,
        # single frame-lines >6000 chars). 3x still catches runaway output.
        if (r.get("n_chunks") or 0) > 3 * r["n_frames_est"] + 1:
            problems.append(f"{r['doc']}: {r['n_chunks']} chunks > "
                            f"3x{r['n_frames_est']} frames + 1")
    if checked == 0:
        return _g("frame_law", "SKIP",
                  f"n_frames_est/duration_s absent on all {missing} records "
                  "(records predate the field — rerun with current driver)")
    status = "FAIL" if problems else ("WARN" if (missing or oversampled) else "PASS")
    detail = "; ".join(problems[:6]) or f"{checked} docs within ±1"
    if oversampled and not problems:
        detail = ("VFR over-sampling (warn): " + "; ".join(oversampled[:5])
                  + f" — {checked - len(oversampled)} within ±1")
    if missing and not problems:
        detail += f" ({missing} records lacked the field)"
    return _g("frame_law", status, detail)


def self_duplication(records):
    """The RR embedding-flush bug class: repeat_factor must be exactly 1.
    Needs no other arm; survives engine upgrades."""
    problems = []
    for r in records:
        h = r.get("chunk_sha256") or []
        if len(h) != len(set(h)):
            problems.append(f"{r['doc']}: {len(h) - len(set(h))} duplicate chunks")
    return _g("self_duplication", "FAIL" if problems else "PASS",
              "; ".join(problems[:6]) or "no duplicate chunks in any doc")


def determinism(rep_records):
    """Across reps ON THE SAME PLATFORM: ordered chunk hashes identical.
    rep_records: list of {doc: record} maps, one per rep. A single rep is
    NOT_RUN (SKIP), not a processing failure: fail-closed moves to the CLAIM
    level — the report stamps evidence_grade SIZING and publishable
    determinism claims stay prohibited (exit-semantics review 2026-08-23)."""
    if len(rep_records) < 2:
        return _g("determinism", "SKIP",
                  f"NOT_RUN — {len(rep_records)} rep(s): determinism not "
                  f"exercised; evidence grade SIZING, no publishable "
                  f"determinism claim")
    base = rep_records[0]
    problems = []
    compared = 0
    for rep_i, rep in enumerate(rep_records[1:], start=2):
        common = set(base) & set(rep)
        for d in sorted(common):
            compared += 1
            if base[d].get("chunk_sha256") != rep[d].get("chunk_sha256"):
                problems.append(f"{d}: chunk hashes differ rep1 vs rep{rep_i}")
            ea, eb = base[d].get("embedding_sha256"), rep[d].get("embedding_sha256")
            if ea and eb and ea != eb:
                problems.append(f"{d}: embedding digests differ rep1 vs rep{rep_i}")
    if compared == 0:
        return _g("determinism", "FAIL", "no common docs across reps")
    return _g("determinism", "FAIL" if problems else "PASS",
              "; ".join(problems[:6]) or
              f"{compared} doc-pairs byte-identical across {len(rep_records)} reps")


def cross_arm(arm_a, arm_b, name_a="A", name_b="B"):
    """Work equivalence between arms (functional bands, not byte parity).
    arm_a/arm_b: {doc: record}. Returns a list of gate dicts."""
    common = sorted(set(arm_a) & set(arm_b))
    if not common:
        return [_g("cross_arm", "FAIL", "no common docs between arms")]
    out = []

    # Banded (2026-08-22): VFR/odd-timestamp sources (old film prints) make
    # the arms' frame extractors legitimately disagree by a few percent —
    # measured up to 6% on archive_films while detections/chunks stayed in
    # band. Exact match = PASS (AMI is exact); <=10% relative = WARN;
    # beyond 10% = FAIL (a real extraction bug, not timestamp jitter).
    have_frames = any(arm_a[d].get("n_frames_est") is not None for d in common)
    exact, warn_fp, fail_fp = 0, [], []
    for d in common:
        a, b = arm_a[d].get("n_frames_est"), arm_b[d].get("n_frames_est")
        if a is None or b is None:
            continue
        if a == b:
            exact += 1
        elif abs(a - b) / max(a, b) <= 0.10:
            warn_fp.append((d, a, b))
        else:
            fail_fp.append((d, a, b))
    out.append(_g("frame_parity",
                  "SKIP" if not have_frames else
                  ("FAIL" if fail_fp else ("WARN" if warn_fp else "PASS")),
                  "n_frames_est absent" if not have_frames else
                  ("; ".join(f"{d}: {a} vs {b} (>10%)" for d, a, b in fail_fp[:5]) or
                   ("; ".join(f"{d}: {a} vs {b}" for d, a, b in warn_fp[:5])
                    + f" — VFR-band; {exact} exact") if warn_fp else
                   f"{exact} docs, frame counts identical")))

    ratios = []
    for d in common:
        a, b = arm_a[d].get("n_detections"), arm_b[d].get("n_detections")
        if a and b:
            ratios.append((d, a / b))
    if ratios:
        outside = [(d, r) for d, r in ratios
                   if not DET_RATIO_WARN[0] <= r <= DET_RATIO_WARN[1]]
        out.append(_g("detection_ratio", "WARN" if outside else "PASS",
                      "; ".join(f"{d}: {r:.2f}" for d, r in outside[:5]) or
                      f"{len(ratios)} docs inside {DET_RATIO_WARN}"))
    else:
        out.append(_g("detection_ratio", "SKIP", "n_detections absent"))

    hard, warn = [], []
    for d in common:
        a, b = arm_a[d].get("n_chunks") or 0, arm_b[d].get("n_chunks") or 0
        if not (a and b):
            continue
        r = a / b
        if not CHUNK_RATIO_HARD[0] <= r <= CHUNK_RATIO_HARD[1]:
            hard.append((d, r))
        elif not CHUNK_RATIO_WARN[0] <= r <= CHUNK_RATIO_WARN[1]:
            warn.append((d, r))
    out.append(_g("chunk_ratio",
                  "FAIL" if hard else ("WARN" if warn else "PASS"),
                  ("; ".join(f"{d}: {r:.2f} OUTSIDE HARD {CHUNK_RATIO_HARD}"
                             for d, r in hard[:5])) or
                  ("; ".join(f"{d}: {r:.2f}" for d, r in warn[:5])) or
                  f"{len(common)} docs inside warn band {CHUNK_RATIO_WARN}"))
    return out


def input_identity(arm_a, arm_b):
    """Both arms must have eaten the same bytes."""
    common = sorted(set(arm_a) & set(arm_b))
    bad = [d for d in common
           if arm_a[d].get("input_sha256") != arm_b[d].get("input_sha256")]
    return _g("input_identity", "FAIL" if bad else "PASS",
              f"differing inputs: {bad[:5]}" if bad else
              f"{len(common)} docs, identical input bytes")


def corpus_pin(records, manifest):
    """Corpus identity: input bytes vs the manifest's sha map — FAIL-CLOSED
    (hardened 2026-08-23). An empty map, a record with no pinned sha, a
    duplicate record, or a drifted sha all FAIL: 'we did not check' must
    never read as 'it passed'."""
    shas = (manifest or {}).get("sha256") or {}
    if not shas:
        return _g("corpus_pin", "FAIL",
                  "manifest carries no sha256 map — unverifiable, fails closed")
    problems = []
    counts = Counter(r["doc"] for r in records)
    dups = sorted(d for d, c in counts.items() if c > 1)
    if dups:
        problems.append(f"duplicate records: {dups[:3]}")
    unpinned = sorted(r["doc"] for r in records if r["doc"] not in shas)
    if unpinned:
        problems.append(f"no pinned sha for: {unpinned[:3]} "
                        f"({len(unpinned)} total)")
    drifted = sorted(r["doc"] for r in records if r["doc"] in shas
                     and shas[r["doc"]].get("sha256") != r.get("input_sha256"))
    if drifted:
        problems.append(f"drifted from manifest: {drifted[:3]} "
                        f"({len(drifted)} total)")
    return _g("corpus_pin", "FAIL" if problems else "PASS",
              "; ".join(problems) if problems else
              f"{len(records)} docs match the pinned shas (fail-closed)")


def chunk_parity_tight(arm_a, arm_b):
    """The haystack-suite's strict parity: per video |a-b| <= 1 chunk; total
    within 5% OR abs diff <= 1. WARN-level here: our arms run different
    detector builds (functional replication, not byte parity), so treat as
    a diagnostic band inside the hard chunk_ratio gate."""
    common = sorted(set(arm_a) & set(arm_b))
    per = [d for d in common
           if abs((arm_a[d].get("n_chunks") or 0) - (arm_b[d].get("n_chunks") or 0)) > 1]
    ta = sum(arm_a[d].get("n_chunks") or 0 for d in common)
    tb = sum(arm_b[d].get("n_chunks") or 0 for d in common)
    total_ok = (abs(ta - tb) <= 1) or (tb and abs(ta / tb - 1) <= 0.05)
    if not per and total_ok:
        return _g("chunk_parity_tight", "PASS",
                  f"per-video |delta|<=1 on all {len(common)}; totals {ta} vs {tb}")
    detail = []
    if per:
        detail.append("per-video >1: " + "; ".join(
            f"{d}: {arm_a[d].get('n_chunks')} vs {arm_b[d].get('n_chunks')}"
            for d in per[:5]))
    if not total_ok:
        detail.append(f"totals {ta} vs {tb} ({ta / tb:.3f})" if tb else "no B total")
    return _g("chunk_parity_tight", "WARN", "; ".join(detail))


def workload_ratio(arm_a, arm_b):
    """Total produced-work ratio (informational, not a gate)."""
    common = set(arm_a) & set(arm_b)
    ta = sum(arm_a[d].get("n_chunks") or 0 for d in common)
    tb = sum(arm_b[d].get("n_chunks") or 0 for d in common)
    return round(ta / tb, 3) if tb else None
