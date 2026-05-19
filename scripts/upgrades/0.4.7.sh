#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.4.7"

cd "$APP_DIR" || exit 1
set_compose_base

printf '\n'
for svc in worker-jobs worker-monitor; do
  run_cmd --try "${CHILD_MARK} Normalizing ${svc} after failed rollouts" normalize_service_scale "$svc" 1
done

run_cmd "${CHILD_MARK} Ensuring Traefik security middlewares" ensure_security_middlewares_file
if is_stack_running; then
  refresh_traefik_security
fi
