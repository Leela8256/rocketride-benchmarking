#!/usr/bin/env bash
# Parse-node A/B on the AWS box (issue #2745 / PR #2746): the same 3.5.0 engine
# twice, only the parse node differs, on the pathology set (one document at a
# time, traced) and optionally the 200-document bulk cell.
#
#   ENGINE_URL_PATCHED  tarball of the experimental release built from the PR
#                       branch (+ #2720, the Linux parse+torch startup fix)
#   PARSE_STOCK_URL     tarball whose parse node is the stock one; its
#                       nodes/parse/libparse.so + java/lib/rocketride-parse.jar
#                       replace the PR's in the control arm
#   CELLS               "pathology" (default) and/or "bulk"
#
#   ENGINE_URL_PATCHED=https://github.com/rocketride-org/rocketride-server/releases/download/server-v3.5.0-parse2745/rocketride-server-v3.5.0-linux-x64.tar.gz \
#   PARSE_STOCK_URL=https://github.com/rocketride-org/rocketride-server/releases/download/server-v3.5.0-prerelease/rocketride-server-v3.5.0-linux-x64.tar.gz \
#   bash run/parse_ab_aws.sh
set -uo pipefail
cd "$(dirname "$0")/.."
: "${ENGINE_URL_PATCHED:?set ENGINE_URL_PATCHED}"
: "${PARSE_STOCK_URL:?set PARSE_STOCK_URL}"
CELLS="${CELLS:-pathology}"
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# 3.5.0: the chunk-duplication fix is upstream, so the 3.3.1 build-time patch
# must stay off (its guard expects exactly one preventDefault and would fail
# the build). The engine binary hash is not known before the first pull.
export ENGINE_VERSION=3.5.0 ENGINE_URL="$ENGINE_URL_PATCHED" ENGINE_SHA256="" RR_DUP_PATCH=0
export DATE BENCH_S3="${BENCH_S3:-s3://rocketride-benchmark-data/leela/bench/parse2745-$DATE}"

run_arm() {   # run_arm <arm> <cell>
  local arm=$1 cell=$2
  if [ "$arm" = stock ]; then export PARSE_STOCK_URL; else export PARSE_STOCK_URL=""; fi
  echo "=== [$cell] arm=$arm parse_stock_url='${PARSE_STOCK_URL}' $(date -u +%H:%M:%SZ)"
  case "$cell" in
    pathology) STAMP="parse2745-${arm}-${TS}" bash run/parse_pathology.sh ;;
    bulk)      STAMP="parse2745-${arm}-${TS}" CELLS="rr-traced" bash run/nodeprofile.sh ;;
    *) echo "unknown cell $cell"; return 1 ;;
  esac
  echo "=== [$cell] arm=$arm done rc=$? $(date -u +%H:%M:%SZ)"
}

for cell in $CELLS; do
  run_arm stock "$cell"
  run_arm patched "$cell"
done
echo "all cells done; results under results/ and $BENCH_S3"
