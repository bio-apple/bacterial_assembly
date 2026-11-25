#!/usr/bin/env bash
# Build the application image. Platform: --platform > PLATFORM > host (ADR 0019).
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=lib/resolve-platform.sh
source "$ROOT/docker/lib/resolve-platform.sh"

usage() {
  echo "usage: $0 [--platform <docker-platform>] [--tag <name:tag>]" >&2
  echo "  supported: linux/amd64 | linux/arm64; see ADR 0019" >&2
  echo "  priority: --platform > PLATFORM env > host uname -m" >&2
  echo "  example: $0" >&2
  echo "           $0 --platform linux/amd64" >&2
  exit 1
}

PLATFORM_FLAG=""
TAG="bacterial-assembly:latest"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)
      [[ $# -ge 2 ]] || usage
      PLATFORM_FLAG=$2
      shift 2
      ;;
    --tag)
      [[ $# -ge 2 ]] || usage
      TAG=$2
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

docker build --platform "$PLATFORM" -t "$TAG" -f "$ROOT/Dockerfile" "$ROOT"
echo "Built ${TAG} (${PLATFORM})"
