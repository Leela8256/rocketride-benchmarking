"""Stable error contract: {"error":{"code","message","request_id"}}."""

from typing import Optional

from fastapi import Response

from service.canonical import canonical_encode

ERROR_STATUS = {
    "unknown_pipeline": 404,
    "server_not_ready": 503,
    "invalid_options": 400,
    "empty_input": 400,
    "unsupported_media_type": 415,
    "payload_too_large": 413,
    "processing_timeout": 504,
    "processing_failed": 500,
    "canonical_encoding_failed": 500,
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, request_id: Optional[str] = None):
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown error code: {code!r}")
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.code]


def error_body(code: str, message: str, request_id: Optional[str]) -> bytes:
    return canonical_encode(
        {"error": {"code": code, "message": message, "request_id": request_id}}
    )


def error_response(err: ServiceError) -> Response:
    return Response(
        content=error_body(err.code, err.message, err.request_id),
        status_code=err.status_code,
        media_type="application/json",
    )
