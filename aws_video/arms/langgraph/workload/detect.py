"""Object detection. Pure computation — no fastapi, no langgraph imports.

Mirrors RocketRide's detect node (rfdetr profile): the RFDETRBase model from
the `rfdetr` package — the exact backend the engine's
ai/common/models/vision/detection.py prefers — at threshold 0.3, emitting
one JSON line per frame in the engine's observed output shape:

    [{"label": ..., "score": ..., "box": {"x1","y1","x2","y2"},
      "centroid": {"x","y"}}, ...]

Serialization is python json.dumps defaults, which is byte-what the engine
emits (verified against the ES2016d capture). Model is a lazy singleton:
loading RF-DETR takes seconds and must happen once, not per frame.
"""

import json
import os
import threading

THRESHOLD = 0.3
_lock = threading.Lock()
_model = None
_classes = None
# Matched posture: at most N concurrent RF-DETR predicts per process (1 =
# mirrors the engine's per-model device lock). 0/unset = the native unlocked
# posture — unchanged. The lock wraps ONLY the predict call: extraction,
# chunking, embedding still overlap.
DETECT_CONCURRENCY = int(os.environ.get("LG_DETECT_CONCURRENCY_PER_PROCESS", "0") or 0)
_inference_sem = threading.Semaphore(DETECT_CONCURRENCY) if DETECT_CONCURRENCY > 0 else None


def _load():
    global _model, _classes
    with _lock:
        if _model is None:
            from rfdetr import RFDETRBase
            try:  # rfdetr >= 1.9 (dict keyed by class id)
                from rfdetr.assets.coco_classes import COCO_CLASSES
            except ImportError:  # older releases
                from rfdetr.util.coco_classes import COCO_CLASSES
            _model = RFDETRBase()
            _classes = COCO_CLASSES
    return _model, _classes


def detect_frame(image) -> str:
    """One frame -> one JSON line of detections (possibly '[]')."""
    model, classes = _load()
    if _inference_sem is not None:
        with _inference_sem:
            det = model.predict(image, threshold=THRESHOLD)
    else:
        det = model.predict(image, threshold=THRESHOLD)
    out = []
    for (x1, y1, x2, y2), score, cls in zip(det.xyxy, det.confidence, det.class_id):
        out.append({
            "label": classes[int(cls)],
            "score": float(score),
            "box": {"x1": float(x1), "y1": float(y1),
                    "x2": float(x2), "y2": float(y2)},
            "centroid": {"x": (float(x1) + float(x2)) / 2,
                         "y": (float(y1) + float(y2)) / 2},
        })
    return json.dumps(out)


def detect_frames(frames) -> list[str]:
    """frames: PNG paths (streamed: one decoded at a time, released before
    the next) or already-decoded images (legacy callers)."""
    out = []
    for f in frames:
        if isinstance(f, (str, os.PathLike)):
            from workload.frames import load_frame
            img = load_frame(f)
            out.append(detect_frame(img))
            del img
        else:
            out.append(detect_frame(f))
    return out
