#!/usr/bin/env bash
set -Eeuo pipefail
systemctl --no-pager --full status \
  bacterial-web.service \
  bacterial-worker-qc.service \
  bacterial-worker-assembly.service
