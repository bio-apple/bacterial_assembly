#!/usr/bin/env bash
# Stop project systemd units only (not PostgreSQL/Redis/Docker/Nginx).
set -Eeuo pipefail
systemctl stop bacterial-worker-assembly.service bacterial-worker-qc.service bacterial-web.service
echo "Stopped bacterial-web / worker-qc / worker-assembly"
