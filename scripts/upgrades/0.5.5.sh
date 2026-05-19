#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.5"

cd "$APP_DIR" || exit 1

printf '\n'
run_cmd "${CHILD_MARK} Repairing CrowdSec firewall bouncer (LAPI + DOCKER-USER)" \
  bash "$APP_DIR/scripts/provision/crowdsec.sh"
