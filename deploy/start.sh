#!/usr/bin/env bash
# Start project systemd units only (not PostgreSQL/Redis/Docker/Nginx).
set -Eeuo pipefail
systemctl start bacterial-web.service bacterial-worker-qc.service bacterial-worker-assembly.service
systemctl --no-pager --full status bacterial-web.service bacterial-worker-qc.service bacterial-worker-assembly.service
