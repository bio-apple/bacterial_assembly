#!/usr/bin/env bash
# Idempotent host install for Web + RQ workers + tool Docker images (ADR 0020).
set -Eeuo pipefail

DEPLOY=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DEPLOY/.." && pwd)

REPO="${ASSEMBLY_REPO:-/opt/bacterial-assembly}"
DATA_ROOT="${ASSEMBLY_DATA_ROOT:-/srv/bacterial-assembly/data}"
ETC_DIR="${ASSEMBLY_ETC:-/etc/bacterial-assembly}"
ENV_FILE="${ETC_DIR}/assembly.env"
USERS_FILE="${ETC_DIR}/users.json"
SERVICE_USER="${ASSEMBLY_SERVICE_USER:-bacterial}"
VENV="${ASSEMBLY_VENV:-${REPO}/.venv}"
MAMBA_ROOT="${ASSEMBLY_MAMBA_ROOT:-${REPO}/.mamba}"
MAMBA_ENV="${ASSEMBLY_MAMBA_ENV:-${MAMBA_ROOT}/envs/validate}"
INSTALL_DEPS=0
START_SERVICES=0
SKIP_MIGRATE=0

usage() {
  cat <<EOF
usage: $0 [options]
  --repo PATH           Install / link repository path (default: ${REPO})
  --data-root PATH      ASSEMBLY_DATA_ROOT (default: ${DATA_ROOT})
  --install-deps        Attempt apt/dnf install of python3, venv, postgres, redis, nginx
  --start               systemctl enable --now web + workers after install
  --skip-migrate        Do not run python -m web.migrate_pg
  -h, --help            Show help
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO=$2; shift 2 ;;
    --data-root) DATA_ROOT=$2; shift 2 ;;
    --install-deps) INSTALL_DEPS=1; shift ;;
    --start) START_SERVICES=1; shift ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    -h|--help) usage ;;
    *) echo "error: unknown argument: $1" >&2; usage ;;
  esac
done

VENV="${ASSEMBLY_VENV:-${REPO}/.venv}"
MAMBA_ROOT="${ASSEMBLY_MAMBA_ROOT:-${REPO}/.mamba}"
MAMBA_ENV="${ASSEMBLY_MAMBA_ENV:-${MAMBA_ROOT}/envs/validate}"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "run as root (needed for user, /etc, systemd, nginx)"
}

os_id() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "${ID:-unknown}"
  else
    echo unknown
  fi
}

install_deps() {
  local id
  id=$(os_id)
  info "Installing OS packages (--install-deps) for ${id}"
  case "$id" in
    ubuntu|debian)
      apt-get update -y
      DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 python3-venv python3-pip \
        postgresql postgresql-contrib redis-server nginx curl ca-certificates
      ;;
    rhel|centos|rocky|almalinux|fedora)
      if command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip postgresql-server redis nginx curl
      else
        yum install -y python3 python3-pip postgresql-server redis nginx curl
      fi
      ;;
    *)
      die "unsupported OS for --install-deps (${id}); install Python 3.10+, PostgreSQL, Redis, Nginx, Docker manually"
      ;;
  esac
}

check_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "host deployment targets Linux (got $(uname -s))"
}

check_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  python3 - <<'PY' || die "Python >= 3.10 required"
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)
PY
  info "Python OK: $(python3 -V)"
}

check_docker() {
  command -v docker >/dev/null 2>&1 || die "docker CLI not found; install Docker Engine"
  docker info >/dev/null 2>&1 || die "cannot talk to Docker Engine (docker info failed)"
  info "Docker Engine OK"
}

check_tool_images() {
  local missing=0 img
  for img in \
    bacterial-assembly-fastp:latest \
    bacterial-assembly-spades:latest \
    bacterial-assembly-unicycler:latest \
    bacterial-assembly-shovill:latest \
    bacterial-assembly-megahit:latest \
    bacterial-assembly-quast:latest
  do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
      echo "missing image: $img" >&2
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    die "build tool images first: cd ${ROOT} && ./docker/tools/build.sh"
  fi
  info "Tool images OK"
}

check_postgres() {
  if command -v pg_isready >/dev/null 2>&1; then
    pg_isready >/dev/null 2>&1 || echo "warn: pg_isready failed (ensure PostgreSQL is running and DATABASE_URL is correct)" >&2
  else
    echo "warn: pg_isready not found; ensure PostgreSQL is installed" >&2
  fi
}

check_redis() {
  if command -v redis-cli >/dev/null 2>&1; then
    redis-cli ping >/dev/null 2>&1 || echo "warn: redis-cli ping failed (ensure Redis is running and REDIS_URL is correct)" >&2
  else
    echo "warn: redis-cli not found; ensure Redis is installed" >&2
  fi
}

ensure_user() {
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "User ${SERVICE_USER} exists"
  else
    info "Creating user ${SERVICE_USER}"
    useradd --system --create-home --home-dir "/var/lib/${SERVICE_USER}" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$SERVICE_USER" || true
    info "User ${SERVICE_USER} in docker group (members can control the Docker Engine — treat as privileged)"
  else
    echo "warn: group docker missing; add ${SERVICE_USER} after installing Docker" >&2
  fi
}

ensure_repo() {
  if [[ -d "$REPO/.git" ]] || [[ -f "$REPO/web/app.py" ]]; then
    info "Repository present at ${REPO}"
  elif [[ "$ROOT" != "$REPO" ]]; then
    info "Linking ${ROOT} -> ${REPO}"
    mkdir -p "$(dirname "$REPO")"
    ln -sfn "$ROOT" "$REPO"
  else
    die "repository not found at ${REPO}"
  fi
}

ensure_data_dirs() {
  info "Ensuring data directories under ${DATA_ROOT}"
  mkdir -p \
    "${DATA_ROOT}/uploads/chunks" \
    "${DATA_ROOT}/uploads/incoming" \
    "${DATA_ROOT}/uploads/validated" \
    "${DATA_ROOT}/jobs" \
    "${DATA_ROOT}/reports" \
    "${DATA_ROOT}/database" \
    "${DATA_ROOT}/temp" \
    "${DATA_ROOT}/archive"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$DATA_ROOT"
}

ensure_venv() {
  if [[ ! -x "${VENV}/bin/python" ]]; then
    info "Creating venv at ${VENV}"
    python3 -m venv "$VENV"
  fi
  info "Installing Python deps (requirements-host.txt)"
  "${VENV}/bin/pip" install --upgrade pip
  "${VENV}/bin/pip" install -r "${REPO}/requirements-host.txt"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$VENV" || true
}

ensure_mamba_validate() {
  if [[ -x "${MAMBA_ENV}/bin/ValidateFastq" ]] || command -v ValidateFastq >/dev/null 2>&1; then
    info "ValidateFastq available"
    return 0
  fi
  if ! command -v micromamba >/dev/null 2>&1 && [[ ! -x "${MAMBA_ROOT}/bin/micromamba" ]]; then
    cat <<EOF >&2
error: ValidateFastq not on PATH and micromamba not found.
Install micromamba, then:
  export MAMBA_ROOT_PREFIX=${MAMBA_ROOT}
  micromamba create -y -p ${MAMBA_ENV} -c conda-forge -c bioconda \\
    python=3.10 biopet-validatefastq=0.1.1 openjdk=8
See docs/host_deployment.md
EOF
    exit 1
  fi
  local mm
  mm=$(command -v micromamba || true)
  [[ -n "$mm" ]] || mm="${MAMBA_ROOT}/bin/micromamba"
  info "Creating micromamba env for ValidateFastq at ${MAMBA_ENV}"
  mkdir -p "$MAMBA_ROOT"
  MAMBA_ROOT_PREFIX="$MAMBA_ROOT" "$mm" create -y -p "$MAMBA_ENV" \
    -c conda-forge -c bioconda \
    python=3.10 biopet-validatefastq=0.1.1 openjdk=8
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$MAMBA_ROOT" || true
}

ensure_env_file() {
  mkdir -p "$ETC_DIR"
  chmod 755 "$ETC_DIR"
  if [[ -f "$ENV_FILE" ]]; then
    info "Keeping existing ${ENV_FILE} (not overwriting secrets)"
  else
    info "Installing ${ENV_FILE} from example (YOU must set secrets)"
    sed \
      -e "s|^ASSEMBLY_REPO=.*|ASSEMBLY_REPO=${REPO}|" \
      -e "s|^ASSEMBLY_DATA_ROOT=.*|ASSEMBLY_DATA_ROOT=${DATA_ROOT}|" \
      -e "s|^ASSEMBLY_USERS_FILE=.*|ASSEMBLY_USERS_FILE=${USERS_FILE}|" \
      -e "s|^ASSEMBLY_SQLITE_PATH=.*|ASSEMBLY_SQLITE_PATH=${DATA_ROOT}/database/jobs.sqlite3|" \
      -e "s|^ASSEMBLY_VENV=.*|ASSEMBLY_VENV=${VENV}|" \
      -e "s|^ASSEMBLY_MAMBA_ENV=.*|ASSEMBLY_MAMBA_ENV=${MAMBA_ENV}|" \
      "${DEPLOY}/assembly.env.example" >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE" || chmod 600 "$ENV_FILE"
  fi
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
  [[ -n "${ASSEMBLY_WEB_SECRET:-}" ]] || die "${ENV_FILE}: set ASSEMBLY_WEB_SECRET (openssl rand -hex 32)"
  [[ "${DATABASE_URL:-}" != *CHANGE_ME* ]] || die "${ENV_FILE}: replace CHANGE_ME in DATABASE_URL"
  [[ -n "${DATABASE_URL:-}" && -n "${REDIS_URL:-}" ]] || die "${ENV_FILE}: DATABASE_URL and REDIS_URL are required"
}

ensure_users_file() {
  if [[ -f "$USERS_FILE" ]]; then
    info "Keeping existing ${USERS_FILE}"
  elif [[ -f "${DEPLOY}/users.json.example" ]]; then
    info "Installing ${USERS_FILE} from users.json.example (replace hashes before production use)"
    cp "${DEPLOY}/users.json.example" "$USERS_FILE"
    chmod 600 "$USERS_FILE"
    chown "$SERVICE_USER":"$SERVICE_USER" "$USERS_FILE"
  else
    die "missing ${USERS_FILE}; create from deploy/users.json.example"
  fi
}

run_migrate() {
  [[ "$SKIP_MIGRATE" -eq 1 ]] && { info "Skipping migrate_pg"; return 0; }
  info "Running database migrations"
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
  export PYTHONPATH="$REPO"
  export PATH="${MAMBA_ENV}/bin:${VENV}/bin:${PATH}"
  sudo -u "$SERVICE_USER" -E env \
    PYTHONPATH="$REPO" \
    PATH="${MAMBA_ENV}/bin:${VENV}/bin:${PATH}" \
    ASSEMBLY_DATA_ROOT="$ASSEMBLY_DATA_ROOT" \
    ASSEMBLY_USERS_FILE="$ASSEMBLY_USERS_FILE" \
    ASSEMBLY_SQLITE_PATH="${ASSEMBLY_SQLITE_PATH:-}" \
    DATABASE_URL="$DATABASE_URL" \
    REDIS_URL="$REDIS_URL" \
    TASK_BACKEND="${TASK_BACKEND:-postgres_rq}" \
    TOOL_RUNTIME="${TOOL_RUNTIME:-docker}" \
    ASSEMBLY_WEB_SECRET="$ASSEMBLY_WEB_SECRET" \
    "${VENV}/bin/python" -m web.migrate_pg
}

install_units() {
  info "Installing systemd units"
  local unit
  for unit in bacterial-web.service bacterial-worker-qc.service bacterial-worker-assembly.service; do
    sed \
      -e "s|/opt/bacterial-assembly/\\.mamba/envs/validate|${MAMBA_ENV}|g" \
      -e "s|/opt/bacterial-assembly/\\.venv|${VENV}|g" \
      -e "s|/opt/bacterial-assembly|${REPO}|g" \
      -e "s|/etc/bacterial-assembly/assembly.env|${ENV_FILE}|g" \
      -e "s|^User=bacterial|User=${SERVICE_USER}|" \
      -e "s|^Group=bacterial|Group=${SERVICE_USER}|" \
      "${DEPLOY}/systemd/${unit}" >"/etc/systemd/system/${unit}"
  done
  systemctl daemon-reload
}

install_nginx() {
  local dest="/etc/nginx/conf.d/bacterial-assembly.conf"
  if [[ -f "$dest" ]]; then
    info "Keeping existing ${dest} (not overwriting)"
    return 0
  fi
  if [[ -d /etc/nginx/conf.d ]]; then
    info "Installing ${dest}"
    cp "${DEPLOY}/nginx/bacterial-assembly.conf" "$dest"
    echo "warn: edit SSL certificate paths in ${dest}, then: nginx -t && systemctl reload nginx" >&2
  else
    echo "warn: /etc/nginx/conf.d missing; copy deploy/nginx/bacterial-assembly.conf manually" >&2
  fi
}

maybe_start() {
  [[ "$START_SERVICES" -eq 1 ]] || { info "Units installed; start with: ${DEPLOY}/start.sh"; return 0; }
  systemctl enable --now bacterial-web.service bacterial-worker-qc.service bacterial-worker-assembly.service
  systemctl --no-pager --full status bacterial-web.service bacterial-worker-qc.service bacterial-worker-assembly.service || true
}

main() {
  require_root
  check_linux
  if [[ "$INSTALL_DEPS" -eq 1 ]]; then
    install_deps
  fi
  check_python
  check_docker
  check_tool_images
  check_postgres
  check_redis
  ensure_user
  ensure_repo
  ensure_data_dirs
  ensure_venv
  ensure_mamba_validate
  ensure_env_file
  ensure_users_file
  run_migrate
  install_units
  install_nginx
  maybe_start
  info "Done. See docs/host_deployment.md"
}

main
