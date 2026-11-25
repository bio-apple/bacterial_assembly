# Shared Docker platform resolution (ADR 0019).
# Source from build scripts; do not execute directly.
#
# Usage:
#   source "$ROOT/docker/lib/resolve-platform.sh"
#   resolve_docker_platform "$optional_cli_platform"
# Sets PLATFORM and PLATFORM_SOURCE; prints one line to stderr.
#
# Priority: CLI --platform > env PLATFORM > host uname -m.
# Supported: linux/amd64 | linux/arm64.

resolve_docker_platform() {
  local from_flag="${1:-}"

  if [[ -n "$from_flag" ]]; then
    PLATFORM=$from_flag
    PLATFORM_SOURCE="--platform"
  elif [[ -n "${PLATFORM:-}" ]]; then
    PLATFORM_SOURCE="PLATFORM"
  else
    local arch
    arch=$(uname -m)
    case "$arch" in
      x86_64|amd64)
        PLATFORM=linux/amd64
        ;;
      aarch64|arm64)
        PLATFORM=linux/arm64
        ;;
      *)
        echo "error: cannot map host arch '${arch}' to a Docker platform; pass --platform linux/amd64 or linux/arm64 (ADR 0019)" >&2
        return 1
        ;;
    esac
    PLATFORM_SOURCE="host"
  fi

  case "$PLATFORM" in
    linux/amd64|linux/arm64) ;;
    *)
      echo "error: --platform must be linux/amd64 or linux/arm64 (got ${PLATFORM}); see ADR 0019" >&2
      return 1
      ;;
  esac

  echo "Using Docker platform ${PLATFORM} (from ${PLATFORM_SOURCE})" >&2
}
