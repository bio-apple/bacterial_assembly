#!/usr/bin/env bash
# Follow journald logs for project units. Extra args pass to journalctl.
set -Eeuo pipefail
exec journalctl -f \
  -u bacterial-web.service \
  -u bacterial-worker-qc.service \
  -u bacterial-worker-assembly.service \
  "$@"
