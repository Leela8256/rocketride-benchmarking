#!/usr/bin/env bash
# JIT staging wrapper, ON THE BOX: env resolution + idle-watchdog keepalive
# around run/stage_films_jit.py; freezes automatically when TARGET is hit.
#
#   nohup bash run/stage_films_jit.sh > ~/logs/stage_films_jit.log 2>&1 < /dev/null &
#
# Resumable: relaunch continues at the first unjournaled candidate.
set -euo pipefail
cd "$(dirname "$0")/.."

export AWS_BIN="$(command -v aws || echo "$HOME/.local/bin/aws")"
[ -x "$AWS_BIN" ] || AWS_BIN="/usr/local/bin/aws"
export FFMPEG="$(command -v ffmpeg || echo "$HOME/bin/ffmpeg")"
[ -x "$FFMPEG" ] || { echo "FATAL: no ffmpeg (fetch_ami.sh installs the static build)" >&2; exit 1; }

# Keepalive: the idle watchdog stops the box during network-bound staging
# (bitten during AMI staging). One busy core reads as "not idle".
( while :; do :; done ) &
KEEPALIVE_PID=$!
trap 'kill $KEEPALIVE_PID 2>/dev/null || true' EXIT

python3 -u run/stage_films_jit.py
python3 -u run/freeze_films.py
