#!/usr/bin/env bash
# Build all tool sidecar images. Platform: --platform > PLATFORM > host (ADR 0019).
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=../lib/resolve-platform.sh
source "$ROOT/docker/lib/resolve-platform.sh"

usage() {
  echo "usage: $0 [--platform <docker-platform>]" >&2
  echo "  supported: linux/amd64 | linux/arm64; see ADR 0019" >&2
  echo "  priority: --platform > PLATFORM env > host uname -m" >&2
  echo "  example: $0" >&2
  echo "           $0 --platform linux/amd64" >&2
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

build_one() {
  local name=$1
  local tag=$2
  docker build --platform "$PLATFORM" \
    -t "bacterial-assembly-${name}:${tag}" \
    -f "$ROOT/docker/tools/${name}/Dockerfile" \
    "$ROOT/docker/tools/${name}"
}

build_one fastp 0.23.4
build_one spades 3.15.5
build_one unicycler 0.5.1
build_one shovill 1.4.2
build_one megahit 1.2.9
build_one quast 5.2.0

echo "Built tool images (${PLATFORM}):"
docker images 'bacterial-assembly-*'
