#!/usr/bin/env bash
# Build Compose application image. Platform: --platform > PLATFORM > host (ADR 0019).
# Does not build tool sidecar images; use ./docker/tools/build.sh for those.
set -Eeuo pipefail
DEPLOY=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DEPLOY/.." && pwd)
# shellcheck source=../docker/lib/resolve-platform.sh
source "$ROOT/docker/lib/resolve-platform.sh"

usage() {
  echo "usage: $0 [--platform <docker-platform>]" >&2
  echo "  supported: linux/amd64 | linux/arm64; see ADR 0019" >&2
  echo "  priority: --platform > PLATFORM env > host uname -m" >&2
  echo "  runs: docker compose build --platform <resolved>" >&2
  exit 1
}

PLATFORM_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)
      [[ $# -ge 2 ]] || usage
      PLATFORM_FLAG=$2
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage
      ;;
  esac
done

resolve_docker_platform "$PLATFORM_FLAG"

cd "$DEPLOY"
docker compose build --platform "$PLATFORM"
echo "Compose build finished (${PLATFORM})"
