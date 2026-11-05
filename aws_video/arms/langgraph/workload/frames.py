"""Frame extraction. Pure computation — no fastapi, no langgraph imports.

Mirrors RocketRide's frame_grabber (interval profile): the engine converts
interval=15 s to fps=1/15 and samples via an ffmpeg-based reader. Same here:
ffmpeg's fps filter, lossless PNG intermediates (JPEG would perturb the
pixels the detector sees), decoded to RGB.

Verified against the engine on ES2016d: both yield 102 frames for 1522 s.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

INTERVAL_S = 15


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg  # bundles a static ffmpeg — same trick the engine uses
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_frames(video_path: str) -> tuple[str, list[str]]:
    """One frame per INTERVAL_S seconds, in time order — returned as PNG
    PATHS in a caller-owned temp dir, not decoded images.

    Memory discipline (films500 post-mortem, 2026-08-24): decoding every
    frame up front held ~2.3 GB per 90-min 1080p film, and 32 in flight
    OOM-killed the service. Frames now live on disk until detection reads
    them one at a time; the caller removes the dir when done."""
    td = tempfile.mkdtemp(prefix="lgframes_")
    out = Path(td)
    subprocess.run(
        [ffmpeg_bin(), "-nostdin", "-loglevel", "error",
         "-i", video_path,
         "-vf", f"fps=1/{INTERVAL_S}",
         "-f", "image2", str(out / "f_%06d.png")],
        check=True)
    return td, [str(p) for p in sorted(out.glob("f_*.png"))]


def load_frame(path: str) -> Image.Image:
    """Decode one extracted PNG to RGB (same pixels the detector saw before)."""
    with Image.open(path) as im:
        return im.convert("RGB").copy()


def cleanup_frames(frames_dir: str) -> None:
    shutil.rmtree(frames_dir, ignore_errors=True)
