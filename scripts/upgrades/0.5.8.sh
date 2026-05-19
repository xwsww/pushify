#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.8"

cd "$APP_DIR" || exit 1

printf '\n'
run_cmd "${CHILD_MARK} Hardening Traefik edge limits (v5)" ensure_security_middlewares_file
run_cmd "${CHILD_MARK} Refreshing origin shield" ensure_origin_shield_file
run_cmd "${CHILD_MARK} Updating CrowdSec flood scenarios" bash "$APP_DIR/scripts/provision/crowdsec.sh"
