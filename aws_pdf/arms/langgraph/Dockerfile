# LangGraph arm — production-style FastAPI service.
#
# linux/amd64 on purpose: the RocketRide engine ships linux-x64 only, and the
# two arms must run on the same architecture to be comparable. On an arm64 Mac
# this builds and runs under emulation — correctness only, never timings.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

# Thread pins are baked in, not left to the entrypoint: they must be set
# before torch/tokenizers initialize. service.config.configure_runtime()
# re-applies them and reports them on /meta.
ENV OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf

WORKDIR /app

# Dependency layer, pinned. pypdf is exact: it defines the offline reference.
RUN pip install --no-cache-dir \
      "fastapi>=0.141" \
      "uvicorn>=0.52" \
      "python-multipart>=0.0.32" \
      "langgraph>=1.2.10" \
      "pypdf==6.15.0" \
      "langchain-text-splitters>=1.1.2" \
      "sentence-transformers>=5.7.0"

# Bake the embedding model INTO the image so runtime needs no network and
# every container embeds with byte-identical weights.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/multi-qa-MiniLM-L6-cos-v1', device='cpu')"

# Runtime must never reach the hub: a cache miss should fail loudly instead of
# silently downloading (possibly different) weights mid-benchmark.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY service/ ./service/
COPY pipelines/ ./pipelines/
COPY workload/ ./workload/
COPY pyproject.toml ./

ENV PIPELINES=document-pdf-v1 \
    PORT=8100 \
    TEMP_DIRECTORY=/tmp

EXPOSE 8100

# One worker IS the deployment shape, not a default.
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
