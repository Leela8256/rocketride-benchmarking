#!/bin/sh
# serve  -> boot the engine in the foreground (default)
# <other> -> exec verbatim
set -eu

case "${1:-serve}" in
  serve)
    # --host is EXPLICIT. Without it the engine binds its default interface,
    # which inside a container is not reachable from the host -- the symptom
    # recorded in CONTEXT_SNAPSHOT 4.6 as "WebSocket upgrade rejected through
    # Docker's published-port proxy". That was very likely this missing flag,
    # not a product defect. Adopted from Ansh's engine image.
    echo "[entrypoint] booting RocketRide engine on ${RR_HOST:-0.0.0.0}:${RR_PORT:-5565}"
    cd "${RR_ENGINE_DIR:-/opt/rocketride/engine}"
    exec ./engine "${RR_ENGINE_DIR:-/opt/rocketride/engine}/ai/eaas.py" \
      --host="${RR_HOST:-0.0.0.0}" --port="${RR_PORT:-5565}"
    ;;
  probe)
    echo "[entrypoint] probe mode is not part of aws_pdf" >&2; exit 1
    ;;
  *)
    exec "$@"
    ;;
esac
