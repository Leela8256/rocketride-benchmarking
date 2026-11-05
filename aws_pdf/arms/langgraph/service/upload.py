"""Upload persistence: stream to a temp file, enforce limits during the copy.

The temp-disk write is a deliberate production cost (spooled uploads
generalize to video) — do not optimize it away.
"""

import os
import tempfile
from typing import Optional, Tuple

from fastapi import UploadFile

from service.errors import ServiceError

CHUNK_BYTES = 1024 * 1024  # 1 MiB


async def persist_upload(
    file: UploadFile,
    temp_directory: str,
    max_upload_bytes: int,
    request_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Stream `file` to a temp file in temp_directory.

    Returns (path, size_bytes). Raises payload_too_large the moment the
    limit is crossed (not after the full copy) and empty_input for empty
    files. Cleans up its own partial file on error; the caller owns
    cleanup of the returned path.
    """
    fd, path = tempfile.mkstemp(dir=temp_directory, prefix="upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_upload_bytes:
                    raise ServiceError(
                        "payload_too_large",
                        f"upload exceeds MAX_UPLOAD_BYTES={max_upload_bytes}",
                        request_id,
                    )
                out.write(chunk)
        if size == 0:
            raise ServiceError("empty_input", "uploaded file is empty", request_id)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, size
