"""FastAPI app factory — transport only.

Nothing here knows a pipeline's topology or modality. The handler is
deliberately the whole flow and nothing more:

    ready-check -> registry.get -> RequestContext (persist upload if present)
    -> prepare_input -> ainvoke -> extract_output -> canonical bytes
    -> raw Response;  finally: temp-file cleanup

Deployment shape:
    uvicorn service.app:app --host 0.0.0.0 --port 8100 --workers 1
"""

import json
import os
import platform
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, File, Form, Request, Response, UploadFile

from service.canonical import CanonicalEncodingError, canonical_encode, canonical_sha256
from service.config import Settings, configure_runtime
from service.errors import ServiceError, error_response
from service.pipeline import RequestContext
from service.registry import PipelineRegistry, RegistryError
from service.schemas import build_envelope
from service.upload import persist_upload

Builders = Dict[str, Callable[[], Any]]


def default_builders() -> Builders:
    from pipelines.document_pdf.adapter import build_document_pdf_pipeline
    from pipelines.mock import build_mock_pipeline

    return {
        "mock-v1": build_mock_pipeline,
        "document-pdf-v1": build_document_pdf_pipeline,
    }


def _workload_versions() -> Dict[str, Any]:
    """Versions that define extraction/embedding output — reported in /meta.

    A pypdf bump changes extracted text and invalidates parity artifacts, so
    the running version must be visible, not assumed.
    """
    info: Dict[str, Any] = {}
    try:
        from workload.document.extract import extractor_info
        from workload.document.extract_pdf import pypdf_version

        info["pypdf"] = pypdf_version()
        info["extractor"] = extractor_info()
    except Exception as exc:  # workload deps absent (e.g. mock-only deploys)
        info["pypdf"] = f"unavailable: {type(exc).__name__}"
    try:
        from workload.document import embed, split

        info["embedding"] = embed.model_info()
        info["split"] = split.effective_split_config()
    except Exception as exc:
        info["embedding"] = f"unavailable: {type(exc).__name__}"
    return info


def create_app(
    settings: Optional[Settings] = None,
    builders: Optional[Builders] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    builders = builders if builders is not None else default_builders()

    # Before any heavy import: pin thread env vars.
    env_pins = configure_runtime(settings)

    registry = PipelineRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry.clear()  # startup must be repeatable
        for name in settings.pipelines:
            if name not in builders:
                raise RegistryError(
                    f"configured pipeline {name!r} has no builder; "
                    f"available: {sorted(builders)}"
                )
            registry.register(name, builders[name]())
        await registry.warmup_all()
        app.state.ready = True
        yield
        app.state.ready = False

    app = FastAPI(title="langgraph-fastapi-service", lifespan=lifespan)
    app.state.ready = False
    app.state.settings = settings
    app.state.env_pins = env_pins
    app.state.registry = registry

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> Response:
        return error_response(exc)

    @app.get("/health/live")
    async def health_live() -> Response:
        return Response(
            content=canonical_encode({"status": "live"}),
            media_type="application/json",
        )

    @app.get("/health/ready")
    async def health_ready() -> Response:
        if not app.state.ready:
            raise ServiceError("server_not_ready", "warm-up not complete")
        return Response(
            content=canonical_encode({"status": "ready"}),
            media_type="application/json",
        )

    @app.get("/meta")
    async def meta() -> Response:
        body = {
            "pid": os.getpid(),
            "ready": bool(app.state.ready),
            "pipelines": registry.names(),
            "uvicorn_workers": settings.uvicorn_workers,
            "executor_workers": settings.executor_workers,
            "max_inflight_requests": settings.max_inflight_requests,
            "request_timeout_seconds": settings.request_timeout_seconds,
            "torch_threads": settings.torch_threads,
            "torch_interop_threads": settings.torch_interop_threads,
            "max_upload_bytes": settings.max_upload_bytes,
            "temp_directory": settings.temp_directory,
            "benchmark_mode": settings.benchmark_mode,
            "env_pins": env_pins,
            "architecture": platform.machine(),
            "workload_versions": _workload_versions(),
        }
        return Response(content=canonical_encode(body), media_type="application/json")

    @app.post("/v1/process/{pipeline}")
    async def process(
        pipeline: str,
        # OPTIONAL at the FastAPI layer on purpose: a required File() would
        # 422 before adapters run, and would forbid future no-file pipelines.
        # Each pipeline enforces its own requirement in prepare_input.
        file: Optional[UploadFile] = File(None),
        options: str = Form("{}"),
        request_id: Optional[str] = Form(None),
    ) -> Response:
        total_start = time.perf_counter_ns()
        rid = request_id or uuid.uuid4().hex

        if not app.state.ready:
            raise ServiceError("server_not_ready", "warm-up not complete", rid)
        if pipeline not in registry:
            raise ServiceError(
                "unknown_pipeline", f"unknown pipeline {pipeline!r}", rid
            )
        adapter = registry.get(pipeline)

        try:
            parsed_options = json.loads(options)
        except json.JSONDecodeError:
            raise ServiceError("invalid_options", "options is not valid JSON", rid)
        if not isinstance(parsed_options, dict):
            raise ServiceError("invalid_options", "options must be a JSON object", rid)

        source_path: Optional[str] = None
        try:
            if file is not None:
                source_path, size_bytes = await persist_upload(
                    file, settings.temp_directory, settings.max_upload_bytes, rid
                )
                context = RequestContext(
                    request_id=rid,
                    pipeline=pipeline,
                    source_path=source_path,
                    filename=file.filename,
                    media_type=file.content_type,
                    size_bytes=size_bytes,
                    options=parsed_options,
                )
            else:
                context = RequestContext(
                    request_id=rid, pipeline=pipeline, options=parsed_options
                )

            state = await adapter.prepare_input(context)
            try:
                final_state = await adapter.ainvoke(state)
                output = await adapter.extract_output(final_state)
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError(
                    "processing_failed", f"pipeline execution failed: {exc}", rid
                ) from exc

            envelope = build_envelope(
                request_id=rid,
                pipeline=pipeline,
                filename=context.filename,
                media_type=context.media_type,
                size_bytes=context.size_bytes,
                output=output,
            )
            try:
                body = canonical_encode(envelope)
            except CanonicalEncodingError as exc:
                raise ServiceError("canonical_encoding_failed", str(exc), rid) from exc
        finally:
            if source_path is not None:
                try:
                    os.unlink(source_path)
                except OSError:
                    pass

        timings_ns = dict(final_state.get("timings_ns") or {})
        timings_ns["total"] = time.perf_counter_ns() - total_start
        server_timing = ",".join(
            f"{name};dur={ns / 1e6:.3f}" for name, ns in sorted(timings_ns.items())
        )
        # Timings go in headers ONLY — never the body.
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "X-Request-ID": rid,
                "X-Pipeline": pipeline,
                "X-Output-SHA256": canonical_sha256(body),
                "Server-Timing": server_timing,
            },
        )

    return app


app = create_app()
