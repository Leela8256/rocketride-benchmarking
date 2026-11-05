# Derive from the audited image; do not resolve the workload dependencies again.
ARG BENCH_LG_BASE=bench-langgraph:baseline-20260921
FROM ${BENCH_LG_BASE}
RUN pip install --no-cache-dir --no-deps yappi==1.7.6
COPY bench_profile_app.py /app/bench_profile_app.py
CMD ["uvicorn", "bench_profile_app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
