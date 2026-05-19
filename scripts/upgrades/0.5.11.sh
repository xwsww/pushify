#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.11"

cd "$APP_DIR" || exit 1

printf '\n'
run_cmd "${CHILD_MARK} Regenerating Traefik security middlewares (v5e + HTTPS toggle)" \
  bash -c 'rm -f "$DATA_DIR/traefik/security-middlewares.yml"; ensure_security_middlewares_file'
run_cmd "${CHILD_MARK} Rebuilding application image (panel 500 fix)" \
  bash -c 'set_compose_base; "${COMPOSE_BASE[@]}" build app worker-jobs worker-monitor'
run_cmd "${CHILD_MARK} Recreating app and Traefik" \
  bash -c 'set_compose_base; "${COMPOSE_BASE[@]}" up -d --force-recreate app worker-jobs worker-monitor traefik'
