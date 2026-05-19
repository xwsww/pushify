#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.7"

cd "$APP_DIR" || exit 1

printf '\n'
run_cmd "${CHILD_MARK} Ensuring Traefik security middlewares (v4d, Cloudflare-aware)" \
  ensure_security_middlewares_file
run_cmd "${CHILD_MARK} Refreshing origin shield / proxy trust" ensure_origin_shield_file
