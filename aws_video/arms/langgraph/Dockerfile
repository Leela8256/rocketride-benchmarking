# LangGraph video-detect arm — for the box (linux/amd64), mirroring the
# RocketRide detect pipe stage-for-stage. Built but NOT part of any run
# until the matched-benchmark phase; smoke-tested locally first.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

# ffmpeg for frame extraction (the Mac smoke uses brew's; the container
# gets the distro build — same fps=1/15 filter semantics).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface \
    TORCH_HOME=/root/.cache/torch

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY workload/ ./workload/
COPY graph.py service.py serve8.py ./

EXPOSE 8200
# Models download at first boot into /root/.cache — mount the shared model
# cache volume in compose so the price is paid once per box.
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8200"]
