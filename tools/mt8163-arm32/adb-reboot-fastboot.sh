#!/usr/bin/env bash
# Fastboot escape is intentionally unavailable on amonet v2.0.0: expdb contains
# the LK-stage kaeru payload and must never receive FASTBOOT_PLEASE.
set -euo pipefail

cat >&2 <<'EOF'
ERROR: fastboot escape is disabled on amonet v2.0.0; expdb is protected.
No partition write or reboot request was issued.
EOF
exit 2
