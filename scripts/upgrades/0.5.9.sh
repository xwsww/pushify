#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.5.9"

cd "$APP_DIR" || exit 1

printf '\n'
run_cmd "${CHILD_MARK} Fixing ACME HTTP-01 (Traefik v5c + cert bootstrap)" \
  ensure_security_middlewares_file
run_cmd "${CHILD_MARK} Recreating Traefik and app" \
  bash -c 'set_compose_base; "${COMPOSE_BASE[@]}" up -d --force-recreate --no-deps traefik app phpmyadmin'
