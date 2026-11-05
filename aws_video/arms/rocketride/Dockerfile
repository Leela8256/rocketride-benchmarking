# RocketRide engine for the VIDEO benchmark — self-hosted, no api.rocketride.ai.
#
# IDENTICAL to aws_pdf/arms/rocketride/Dockerfile (the PDF arm) except the
# probe fixtures are not copied: the same engine binary, the same SHA pin, the
# same two 3.3.1 patches. Keeping the images byte-close is deliberate — the
# fewer differences between the PDF and video engines, the fewer variables.
#
# VIDEO NEEDS NO EXTRA SYSTEM PACKAGES (verified against the 3.3.1 tree):
#   - audio_transcribe = faster-whisper (CTranslate2 + PyAV; PyAV bundles ffmpeg)
#   - frame_grabber decodes via the engine's own ai/common/avi/ reader, whose
#     requirements pull imageio_ffmpeg — a pip wheel that SHIPS its own ffmpeg
#   - detect = the rfdetr pip package (+timm); checkpoint lands in the engine
#     model cache
# All of these are installed BY THE ENGINE at pipeline start, not baked here,
# so the first `use()` of the video pipe on a fresh volume downloads several
# GB (torch tree, whisper weights, rf-detr-base.pth). That cost is one-time
# per model-cache volume and must always be treated as warm-up, never measured.
#
# The engine ships linux-x64 only; on an arm64 Mac this runs under emulation:
# correctness only, never timings.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

# libc++ is a hard runtime dependency of the C++ engine binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libc++1 libc++abi1 procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV ENGINE_DIR=/opt/rocketride/engine \
    ROCKETRIDE_PORT=5565 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Pinned release + checksum. A silent engine swap would invalidate every
# captured artifact, so a mismatch must fail the build. ENGINE_SHA256 is the
# hash of the EXTRACTED `engine` BINARY, not the tarball (see the PDF arm).
ARG ENGINE_VERSION=3.3.1
ARG ENGINE_URL=https://github.com/rocketride-org/rocketride-server/releases/download/server-v3.3.1/rocketride-server-v3.3.1-linux-x64.tar.gz
ARG ENGINE_SHA256=95768e2640df2d34dd6dfea2e456f36da03ad80b091f9d057c116dfe748d9747

RUN mkdir -p "$ENGINE_DIR" \
    && curl -fsSL --retry 3 "$ENGINE_URL" -o /tmp/engine.tgz \
    && tar -xzf /tmp/engine.tgz -C "$ENGINE_DIR" \
    && rm /tmp/engine.tgz \
    && if [ ! -f "$ENGINE_DIR/engine" ]; then \
         inner="$(find "$ENGINE_DIR" -maxdepth 2 -name engine -type f | head -1)"; \
         [ -n "$inner" ] && mv "$(dirname "$inner")"/. "$ENGINE_DIR"/ 2>/dev/null || true; \
       fi \
    && test -f "$ENGINE_DIR/engine" && test -f "$ENGINE_DIR/ai/eaas.py" \
    && echo "${ENGINE_SHA256}  ${ENGINE_DIR}/engine" | sha256sum -c - \
    && chmod +x "$ENGINE_DIR/engine"

# ---------------------------------------------------------------------------
# BOOT FIX for 3.3.1 (see aws_pdf/arms/rocketride/Dockerfile for the full
# story): the release pins onnxruntime-gpu==1.20.1, which was never published
# to PyPI, and the engine's all-or-nothing constraints compile refuses to boot
# over it. For VIDEO this pin is not incidental: it sits in
# nodes/audio_transcribe/requirements.txt and requirements_whisper.txt — the
# exact nodes this benchmark runs — so the fix is load-bearing here, not
# hygiene. Every *.txt moves together or constraint compilation conflicts.
RUN find "$ENGINE_DIR" -name '*.txt' -exec \
      sed -i 's/onnxruntime-gpu==1\.20\.1/onnxruntime-gpu==1.20.2/g' {} + \
    && echo "--- onnxruntime-gpu pins after patch (must all be 1.20.2):" \
    && (grep -rn "onnxruntime-gpu==" "$ENGINE_DIR" --include='*.txt' || true) \
    && ! grep -rq "onnxruntime-gpu==1\.20\.1" "$ENGINE_DIR" --include='*.txt'
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BUG_CHUNK_DUPLICATION correction (RocketRide 3.3.1) — same patch as the PDF
# arm, and it applies unchanged to video: the video pipe ends in the SAME
# embedding_transformer node whose flush path double-emits batches. Measured
# on 987 PDFs: 51 docs returned [A,B,C,A,B,C]. RR_DUP_PATCH=0 builds stock.
# Describe the tested system as "RocketRide 3.3.1 with the documented
# embedding-transformer duplication correction" — never as stock 3.3.1.
ARG RR_DUP_PATCH=1
RUN if [ "$RR_DUP_PATCH" = "1" ]; then \
      F="$(find "$ENGINE_DIR" -path '*/nodes/embedding_transformer/IInstance.py' \
            -type f | head -1)" \
      && test -n "$F" \
      && [ "$(grep -c 'preventDefault' "$F")" = "1" ] \
      && awk '{ \
           print; \
           if (prev ~ /# Flush the documents$/ && \
               $0 ~ /^        self\._flushDocuments\(\)$/) { \
             print "        # BENCH PATCH BUG_CHUNK_DUPLICATION:"; \
             print "        # Flush already delivered this batch downstream."; \
             print "        return self.preventDefault()"; \
           } \
           prev=$0; \
         }' "$F" > "$F.patched" \
      && mv "$F.patched" "$F" \
      && [ "$(grep -c 'preventDefault' "$F")" = "2" ] \
      && echo "BUG_CHUNK_DUPLICATION patch applied to $F"; \
    else echo "BUG_CHUNK_DUPLICATION patch SKIPPED (stock 3.3.1)"; fi

# ---------------------------------------------------------------------------
# PR #2197 — fix(embedding): batch sentences by length so padding stops wasting
# compute. _encode_local batched in arrival order while tokenization pads every
# batch to its longest member, so one long chunk made the rest of its batch pay
# the long one's cost. Upstream sentence-transformers sorts by length first;
# this build did not. Stepan's measured gains scale with sentences per encode()
# call: -1% at 16, +30% at 64, +70% at 256. Our AMI docs carry ~85 chunks each.
#
# OFF by default so the stock image is unchanged; set RR_LENSORT_PATCH=1 to
# build the A/B comparison image.
ARG RR_LENSORT_PATCH=0
COPY patches/lensort_2197.py /tmp/lensort_2197.py
RUN if [ "$RR_LENSORT_PATCH" = "1" ]; then \
      python3 /tmp/lensort_2197.py "$ENGINE_DIR"; \
    else echo "PR2197 length-sorted batching SKIPPED (arrival-order batching)"; fi

LABEL benchmark.rocketride.engine_version="3.3.1" \
      benchmark.rocketride.duplication_patch="preventDefault-after-embedding-flush" \
      benchmark.rocketride.lensort_patch_pr2197="${RR_LENSORT_PATCH}" \
      benchmark.rocketride.workload="video"
# ---------------------------------------------------------------------------

# Client SDK pinned to the version the PDF benchmark verified against.
RUN pip install --no-cache-dir "rocketride==1.3.0"

WORKDIR /work

ENV ROCKETRIDE_URI=ws://127.0.0.1:5565/task/service \
    ROCKETRIDE_APIKEY=local-dev \
    RR_ENGINE_DIR=/opt/rocketride/engine \
    RR_PORT=5565

EXPOSE 5565

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["serve"]
