# The bench CLIENT — a third container that drives BOTH arms over the network.
#
# WHY THIS EXISTS: the RocketRide driver used to run inside the engine's own
# container, so its SDK file reads, WebSocket framing and JSON deserialization
# were all charged to RocketRide's cgroup, while LangGraph's driver ran on the
# host and was charged nothing. Every M7 comparison was contaminated.
#
# It also equalizes the transfer path: from here BOTH arms receive their bytes
# over a network hop (WebSocket to the engine, HTTP to LangGraph) instead of
# RocketRide reading container-local disk while LangGraph paid for an upload.
#
# This container is pinned to its OWN cores (CLIENT_CPUSET) so the client can
# never steal CPU from the arm it is measuring.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# The only client dependency. Pinned: an SDK change alters framing and upload
# behaviour, which is measurement, not workload.
RUN pip install --no-cache-dir "rocketride==1.3.0"

WORKDIR /bench
COPY lg_driver.py lg_profile_capture.py rr_driver.py profile_capture.py profile_task.py cgroup_sampler.py verify_output.py ./

# Corpus, results and the pipe are mounted at run time, never baked in.
ENTRYPOINT ["python3"]
