#!/usr/bin/env bash
# Build all tool sidecar images (linux/amd64).
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PLATFORM=${PLATFORM:-linux/amd64}

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

echo "Built tool images:"
docker images 'bacterial-assembly-*'
