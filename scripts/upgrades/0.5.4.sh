#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.4"

cd "$APP_DIR" || exit 1
set_compose_base

printf '\n'
run_cmd "${CHILD_MARK} Restoring Traefik security middlewares (v4b)" ensure_security_middlewares_file
if is_stack_running; then
  run_cmd "${CHILD_MARK} Recreating app" \
    "${COMPOSE_BASE[@]}" up -d --force-recreate --no-deps app
  refresh_traefik_security
  printf "${YEL}%s Redeploy deployments after this upgrade.${NC}\n" "${CHILD_MARK}"
fi
