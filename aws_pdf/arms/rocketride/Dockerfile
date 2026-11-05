# RocketRide arm — self-hosted engine, no api.rocketride.ai, no network at run.
#
# The engine ships linux-x64 only, which is why BOTH arms are pinned to
# linux/amd64. On an arm64 Mac this runs under emulation: correctness only,
# never timings.
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
# captured artifact, so a mismatch must fail the build.
#
# ENGINE_SHA256 is the hash of the EXTRACTED `engine` BINARY, not of the
# tarball — that is what the prior benchmark harness pins, and it is the
# artifact that actually determines behavior. The tarball is flat (ai/,
# engine, lib/, nodes/, ...), so do NOT strip components.
#
# 3.3.1 (was 3.2.1): the Mac cross-arm validation — chunk hashes matching the
# LG-tika arm byte-exactly — was done against native 3.3.1. Building 3.2.1 here
# would mean the box engine is not the engine that work validated. SHA is of
# the extracted linux-x64 `engine` binary, verified 2026-08-14.
ARG ENGINE_VERSION=3.3.1
ARG ENGINE_URL=https://github.com/rocketride-org/rocketride-server/releases/download/server-v3.3.1/rocketride-server-v3.3.1-linux-x64.tar.gz
# Empty ENGINE_SHA256 = do not verify, print the hash instead (experimental
# releases whose binary hash is not known before the first pull). Provenance
# still records the hash the container actually ran (engine_sha.txt).
ARG ENGINE_SHA256=95768e2640df2d34dd6dfea2e456f36da03ad80b091f9d057c116dfe748d9747
ARG ENGINE_ARCHIVE_SHA256=

RUN mkdir -p "$ENGINE_DIR" \
    && curl -fsSL --retry 3 "$ENGINE_URL" -o /tmp/engine.tgz \
    && if [ -n "$ENGINE_ARCHIVE_SHA256" ]; then \
         echo "${ENGINE_ARCHIVE_SHA256}  /tmp/engine.tgz" | sha256sum -c -; fi \
    && tar -xzf /tmp/engine.tgz -C "$ENGINE_DIR" \
    && rm /tmp/engine.tgz \
    # Some release tarballs nest a top-level dir; flatten if so (from Ansh's
    # image). Runs before the checksum so the hash is always taken at the
    # same path regardless of archive layout.
    && if [ ! -f "$ENGINE_DIR/engine" ]; then \
         inner="$(find "$ENGINE_DIR" -maxdepth 2 -name engine -type f | head -1)"; \
         [ -n "$inner" ] && mv "$(dirname "$inner")"/. "$ENGINE_DIR"/ 2>/dev/null || true; \
       fi \
    && test -f "$ENGINE_DIR/engine" && test -f "$ENGINE_DIR/ai/eaas.py" \
    && if [ -n "$ENGINE_SHA256" ]; then \
         echo "${ENGINE_SHA256}  ${ENGINE_DIR}/engine" | sha256sum -c -; \
       else echo "ENGINE_SHA256 not pinned; extracted engine binary:"; sha256sum "$ENGINE_DIR/engine"; fi \
    && chmod +x "$ENGINE_DIR/engine"

# ---------------------------------------------------------------------------
# Parse-node A/B (issue #2745 / PR #2746). PARSE_STOCK_URL names a second
# release tarball whose parse node (nodes/parse/libparse.so and
# java/lib/rocketride-parse.jar) REPLACES the one in ENGINE_URL, so the
# control arm runs the identical engine with only the parse node swapped.
# Empty = keep the parse node the engine tarball shipped.
ARG PARSE_STOCK_URL=
RUN if [ -n "$PARSE_STOCK_URL" ]; then \
      mkdir -p /tmp/parse_stock \
      && curl -fsSL --retry 3 "$PARSE_STOCK_URL" -o /tmp/parse_stock.tgz \
      && tar -xzf /tmp/parse_stock.tgz -C /tmp/parse_stock \
           --wildcards '*nodes/parse/libparse.so' '*java/lib/rocketride-parse.jar' \
      && SO="$(find /tmp/parse_stock -name libparse.so | head -1)" \
      && JAR="$(find /tmp/parse_stock -name rocketride-parse.jar | head -1)" \
      && test -n "$SO" && test -n "$JAR" \
      && test -f "$ENGINE_DIR/nodes/parse/libparse.so" && test -f "$ENGINE_DIR/java/lib/rocketride-parse.jar" \
      && cp "$SO" "$ENGINE_DIR/nodes/parse/libparse.so" \
      && cp "$JAR" "$ENGINE_DIR/java/lib/rocketride-parse.jar" \
      && rm -rf /tmp/parse_stock /tmp/parse_stock.tgz \
      && echo "parse node REPLACED from $PARSE_STOCK_URL" \
      && sha256sum "$ENGINE_DIR/nodes/parse/libparse.so" "$ENGINE_DIR/java/lib/rocketride-parse.jar"; \
    else echo "parse node: as shipped in the engine tarball"; \
      sha256sum "$ENGINE_DIR/nodes/parse/libparse.so" "$ENGINE_DIR/java/lib/rocketride-parse.jar" 2>/dev/null || true; fi
LABEL benchmark.rocketride.parse_stock_url="${PARSE_STOCK_URL}"

# ---------------------------------------------------------------------------
# WORKAROUND for a RocketRide engine bug (present in 3.3.0 AND 3.3.1).
#
# ai/common/models/gliner/requirements_gliner.txt pins:
#     onnxruntime-gpu==1.20.1 ; platform_system != 'Darwin'
#     onnxruntime==1.20.1     ; platform_system == 'Darwin'
#
# onnxruntime-gpu 1.20.1 WAS NEVER PUBLISHED to PyPI (1.20.0 and 1.20.2 exist);
# onnxruntime 1.20.1 does exist. So the Darwin branch resolves and the Linux
# branch cannot. The engine compiles ALL requirements files at boot, so this
# unsatisfiable pin -- in an OPT-IN NER feature this benchmark never loads --
# makes the engine fail to start on ANY Linux host:
#     Failed to compile constraints: no solution found ... onnxruntime-gpu==1.20.1
#
# Bumping only that pin to the nearest published patch. Nothing else is
# touched, and the pipeline under test never imports GLiNER. Remove once
# upstream fixes it. See aws_pdf/findings/rr_linux_boot.md.
# The pin appears in FIVE requirements files, across BOTH ai/ and nodes/:
#   ai/common/models/{audio/requirements_whisper,vision/requirements_pose,
#                     gliner/requirements_gliner}.txt
#   nodes/{audio_transcribe,anonymize}/requirements.txt
# All must move together -- patching only some produces a WORSE failure,
# "you require ==1.20.1 and ==1.20.2", because constraint compilation is
# global across every requirements file in the tree.
# Scope is every *.txt (matching Ansh's image), not just requirements*.txt, so
# a pin in any constraints-style file is caught too. find -exec rather than
# `grep -rl | xargs sed`: once upstream fixes the pin, grep matches nothing and
# bare xargs would run sed with no file, blocking on stdin and hanging the build.
RUN find "$ENGINE_DIR" -name '*.txt' -exec \
      sed -i 's/onnxruntime-gpu==1\.20\.1/onnxruntime-gpu==1.20.2/g' {} + \
    && echo "--- onnxruntime-gpu pins after patch (must all be 1.20.2):" \
    && (grep -rn "onnxruntime-gpu==" "$ENGINE_DIR" --include='*.txt' || true) \
    && ! grep -rq "onnxruntime-gpu==1\.20\.1" "$ENGINE_DIR" --include='*.txt'
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BUG_CHUNK_DUPLICATION correction (RocketRide 3.3.1).
#
# nodes/embedding_transformer/IInstance.py calls _flushDocuments(), which
# already delivers the batch downstream -- and then falls through to the
# node's default forwarding action, emitting the SAME document list a second
# time. Measured on 987 real PDFs: 51 documents came back as [A,B,C,A,B,C],
# repeat_factor exactly 2, while LangGraph showed 0 of 987. That inflated
# RocketRide's work by ~40% and its embedding cost with it.
#
# preventDefault() after the flush suppresses the duplicate emission.
#
# This MODIFIES THE PRODUCT UNDER TEST, so it is a build ARG: set
# RR_DUP_PATCH=0 to build stock 3.3.1 and quantify the difference. Whichever
# was built is recorded in the image label and in run provenance.
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
# batch to its longest member. OFF by default; RR_LENSORT_PATCH=1 builds the
# A/B image. Same patch script as the video arm.
ARG RR_LENSORT_PATCH=0
COPY patches/lensort_2197.py /tmp/lensort_2197.py
RUN if [ "$RR_LENSORT_PATCH" = "1" ]; then \
      python3 /tmp/lensort_2197.py "$ENGINE_DIR"; \
    else echo "PR2197 length-sorted batching SKIPPED (arrival-order batching)"; fi

LABEL benchmark.rocketride.lensort_patch_pr2197="${RR_LENSORT_PATCH}"

LABEL benchmark.rocketride.engine_version="${ENGINE_VERSION}" \
      benchmark.rocketride.engine_url="${ENGINE_URL}" \
      benchmark.rocketride.archive_sha256="${ENGINE_ARCHIVE_SHA256}" \
      benchmark.rocketride.duplication_patch="${RR_DUP_PATCH}"
# ---------------------------------------------------------------------------

# Client SDK pinned to the version the probe was verified against.
RUN pip install --no-cache-dir "rocketride==1.3.0"

# No .pipe baked in: the bench CLIENT owns the pipeline definition and
# sends it with use(). A second copy here drifted from the canonical
# pipe/benchmark_pdf.pipe within a day of being created.
WORKDIR /work
COPY data/ ./data/

ENV ROCKETRIDE_URI=ws://127.0.0.1:5565/task/service \
    ROCKETRIDE_APIKEY=local-dev \
    RR_ENGINE_DIR=/opt/rocketride/engine \
    RR_PORT=5565

EXPOSE 5565

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# The engine runs as a long-lived service; the driver connects to it.
CMD ["serve"]
