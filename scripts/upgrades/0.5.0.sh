#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.0"

cd "$APP_DIR" || exit 1
set_compose_base

printf '\n'
run_cmd "${CHILD_MARK} Ensuring Traefik security middlewares (v4)" ensure_security_middlewares_file
if is_stack_running; then
  run_cmd "${CHILD_MARK} Recreating app (panel in-flight limits)" \
    "${COMPOSE_BASE[@]}" up -d --force-recreate --no-deps app phpmyadmin
  refresh_traefik_security
fi
