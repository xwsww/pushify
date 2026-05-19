#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib.sh"

init_script_logging "upgrade-0.4.6"

printf '\n'
run_cmd "${CHILD_MARK} Ensuring Traefik security middlewares" ensure_security_middlewares_file

if is_stack_running; then
  refresh_traefik_security
else
  printf "${YEL}Stack is not running; Traefik will pick up config on next start.${NC}\n"
fi
