#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

init_script_logging "start"

usage(){
  cat <<USG
Usage: start.sh [--components <csv>] [--no-migrate] [--timeout <sec>] [-v|--verbose] [-h|--help]

Start the /pushify/ stack (dev or prod auto-detected).

  --components <csv>
                    Comma-separated list of services to start (${VALID_COMPONENTS//|/, })
  --no-migrate      Skip running database migrations after start
  --timeout <sec>   Max seconds to wait for app to become healthy (default: 300)
  -v, --verbose     Enable verbose output
  -h, --help        Show this help
USG
  exit 0
}

run_migrations=1
timeout=300
comps=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --components)
      comps="$2"
      IFS=',' read -ra _st_secs <<< "$comps"
      for comp in "${_st_secs[@]}"; do
        comp="${comp// /}"
        [[ -z "$comp" ]] && continue
        if ! validate_component "$comp"; then
          exit 1
        fi
      done
      shift 2
      ;;
    --timeout) timeout="$2"; shift 2 ;;
    --no-migrate) run_migrations=0; shift ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help) usage ;;
    *) err "Unknown option: $1"; usage ;;
  esac
done

cd "$APP_DIR" || { err "App dir not found: $APP_DIR"; exit 1; }

wait_for_docker() {
  local attempts=0
  while (( attempts < 10 )); do
    docker info >/dev/null 2>&1 && return 0
    sleep 1
    ((attempts+=1))
  done
  err "Docker not accessible. Is the daemon running?"
  return 1
}

wait_for_app_health() {
  local timeout_sec="$1"
  local deadline=$((SECONDS + timeout_sec))
  local status container

  while (( SECONDS < deadline )); do
    container="$(docker ps -a --filter "label=com.docker.compose.project=devpush" --filter "label=com.docker.compose.service=app" -q | head -1 || true)"
    if [[ -n "$container" ]]; then
      status="$(docker inspect --format '{{.State.Status}}{{if .State.Health}}:{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null || true)"
      case "$status" in
        running:healthy) return 0 ;;
        restarting|exited:*|dead*) break ;;
      esac
    fi
    sleep 2
  done

  dump_app_start_failure "$container"
  return 1
}

# Validate Docker availability
printf '\n'
check_disk_space 2 15
run_cmd "Waiting for Docker to be ready" wait_for_docker

# Default data directory
mkdir -p -m 0750 "$DATA_DIR/traefik" "$DATA_DIR/upload" "$DATA_DIR/registry"
if [[ "$ENVIRONMENT" == "production" ]]; then
  service_user="$(default_service_user)"
  chown -R "$service_user:$service_user" "$DATA_DIR" || true
fi

# Determine service user/group
set_service_ids

# Ensure registry files exist (catalog.json and overrides.json)
printf '\n'
printf "Ensuring registry files exist\n"
write_registry_files

# Backfill newly required generated secrets for upgraded installs.
ensure_mariadb_env "$ENV_FILE"
ensure_phpmyadmin_env "$ENV_FILE"
ensure_deployment_resource_env "$ENV_FILE"
sync_deployment_resources_for_host "$ENV_FILE"

# Validate env
validate_env "$ENV_FILE"
ensure_acme_json
ensure_security_middlewares_file
ensure_origin_shield_file
ensure_traefik_log_dir

# Build compose args
set_compose_base

# First start on a fresh install: images are not built yet.
app_image="$("${COMPOSE_BASE[@]}" images -q app 2>/dev/null | head -1 || true)"
if [[ -z "$app_image" ]]; then
  printf '\n'
  run_cmd "Building application images (first start)" bash "$SCRIPT_DIR/compose.sh" build app worker-jobs worker-monitor
fi

# Start stack
printf '\n'
component_in_target() {
  local target="$1"
  [[ -z "$comps" ]] && return 0
  IFS=',' read -ra _target_secs <<< "$comps"
  for sec in "${_target_secs[@]}"; do
    sec="${sec// /}"
    [[ "$sec" == "$target" ]] && return 0
  done
  return 1
}

if [[ -n "$comps" ]]; then
  IFS=',' read -ra _selected_services <<< "$comps"
  selected_services=()
  for service in "${_selected_services[@]}"; do
    service="${service// /}"
    [[ -n "$service" ]] && selected_services+=("$service")
  done
  run_cmd "Starting selected services" "${COMPOSE_BASE[@]}" up -d "${selected_services[@]}"
elif is_stack_running; then
  run_cmd "Ensuring services are running" "${COMPOSE_BASE[@]}" up -d --remove-orphans
else
  run_cmd "Starting services" "${COMPOSE_BASE[@]}" up -d --remove-orphans
fi

# Wait for app container to be healthy
if component_in_target "app"; then
  printf '\n'
  run_cmd "Waiting for app to be ready" wait_for_app_health "$timeout"
fi

# Run migrations when appropriate
if ((run_migrations==1)) && component_in_target "app"; then
  run_cmd "${CHILD_MARK} Running database migrations" bash "$SCRIPT_DIR/db-migrate.sh"
fi

# Success message
printf '\n'
printf "${GRN}Stack started. ✔${NC}\n"
