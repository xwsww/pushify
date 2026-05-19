#!/usr/bin/env bash
# Rebuild app/worker images from /opt/devpush and recreate containers (after git pull).
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

init_script_logging "reload-app"

cd "$APP_DIR" || { err "App dir not found: $APP_DIR"; exit 1; }

set_compose_base
printf '\n'
run_cmd "Building application images" "${COMPOSE_BASE[@]}" build app worker-jobs worker-monitor
run_cmd "Recreating application containers" \
  "${COMPOSE_BASE[@]}" up -d --force-recreate app worker-jobs worker-monitor
printf "${GRN}Application reloaded from current checkout.${NC}\n"
