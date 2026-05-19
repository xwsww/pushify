#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

[[ $EUID -eq 0 ]] || { printf "This script must be run as root (sudo).\n" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

init_script_logging "update-apply"

on_error_hook() {
  cd "$APP_DIR" 2>/dev/null || true
  set_compose_base 2>/dev/null || true
  for svc in worker-jobs worker-monitor app; do
    normalize_service_scale "$svc" 1 2>/dev/null || true
  done
  printf "${YEL}Inspect logs: %s/scripts/compose.sh logs worker-jobs${NC}\n" "$APP_DIR"
}

usage(){
  cat <<USG
Usage: update-apply.sh [--ref <tag>] [--all | --components <csv> | --full] [--no-migrate] [--no-security] [--no-telemetry] [--yes|-y] [--verbose]

Apply a fetched update: validate, pull images, rollout, migrate, and record version.

  --ref <tag>       Git tag to record (best-effort if omitted)
                    Default update scope refreshes app and workers unless overridden by flags or upgrade metadata
  --all             Update app, workers, and traefik (app,worker-jobs,worker-monitor,traefik)
  --components <csv>
                    Comma-separated list of services to update (${VALID_COMPONENTS//|/, })
  --full            Full stack update (down whole stack, then up). Causes downtime
  --backup          Create a backup before applying the update (recommended)
  --no-migrate      Do not run DB migrations after app update
  --no-security     Skip host security updates (sysctl, CrowdSec)
  --no-telemetry    Do not send telemetry
  --yes, -y         Non-interactive yes to prompts
  -v, --verbose     Enable verbose output for debugging
  -h, --help        Show this help
USG
  exit 0
}

# Parse CLI flags
ref=""; comps=""; do_all=0; do_full=0; do_backup=0; migrate=1; security=1; yes=0; skip_components=0; telemetry=1
[[ "${NO_TELEMETRY:-0}" == "1" ]] && telemetry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) ref="$2"; shift 2 ;;
    --all) do_all=1; shift ;;
    --components)
      comps="$2"
      IFS=',' read -ra _ua_secs <<< "$comps"
      for comp in "${_ua_secs[@]}"; do
        comp="${comp// /}"
        [[ -z "$comp" ]] && continue
        if ! validate_component "$comp"; then
          exit 1
        fi
      done
      shift 2
      ;;
    --full) do_full=1; shift ;;
    --backup) do_backup=1; shift ;;
    --no-migrate) migrate=0; shift ;;
    --no-security) security=0; shift ;;
    --no-telemetry) telemetry=0; shift ;;
    --yes|-y) yes=1; shift ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help) usage ;;
    *) err "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if ((do_full==1)) && { ((do_all==1)) || [[ -n "$comps" ]]; }; then
  err "--full cannot be combined with --all or --components"
  exit 1
fi

if [[ "$ENVIRONMENT" == "development" ]]; then
  err "This script is for production only. For development, simply pull code with git. More information: https://github.com/xwsww/pushify#development"
  exit 1
fi

cd "$APP_DIR" || { err "App dir not found: $APP_DIR"; exit 1; }

# Determine service user/group
set_service_ids

# Refresh registry files (catalog.json and overrides.json)
printf '\n'
printf "Refreshing registry files\n"
write_registry_files

# Ensure version.json exists
if [[ ! -f "$VERSION_FILE" ]]; then
  err "version.json not found. Run install.sh first."
  exit 1
fi

# Backfill newly required generated secrets for upgraded installs.
ensure_mariadb_env "$ENV_FILE"
ensure_postgres_storage_env "$ENV_FILE"
ensure_phpmyadmin_env "$ENV_FILE"
prepare_shell_scripts "$APP_DIR"

# Validate environment variables
validate_env "$ENV_FILE"

# Ensure Traefik TLS + security file provider config exists
ensure_acme_json
ensure_security_middlewares_file
ensure_origin_shield_file
ensure_traefik_log_dir
if [[ -f "$APP_DIR/scripts/provision/host-security.sh" && "$security" == "1" ]]; then
  run_cmd "${CHILD_MARK} Ensuring host security (sysctl, CrowdSec)" \
    bash "$APP_DIR/scripts/provision/host-security.sh"
fi

# Build compose arguments
set_compose_base

# Version comparison helpers
ver_lt() {
  [[ "$1" == "$2" ]] && return 1
  [[ "$(printf '%s\n%s' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}
ver_lte() {
  [[ "$1" == "$2" ]] && return 0
  ver_lt "$1" "$2"
}

# Versioned file helper
default_components="app"
meta_full=0
meta_components=""
meta_reason=""

get_versioned_files() {
  local current_ver="$1"
  local target_ver="$2"
  local base_dir="$3"
  local ext="$4"

  [[ -d "$base_dir" ]] || return 0

  current_ver="${current_ver%%-*}"
  target_ver="${target_ver%%-*}"

  while IFS= read -r file; do
    local ver
    ver=$(basename "$file" ".$ext")
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    if ver_lt "$current_ver" "$ver" && ver_lte "$ver" "$target_ver"; then
      printf '%s\n' "$file"
    fi
  done < <(find "$base_dir" -name "*.${ext}" -type f 2>/dev/null | sort -V)
}

# Run upgrade hooks for version transitions
run_upgrade_hooks() {
  local scripts=("$@")

  local count=${#scripts[@]}
  ((count==0)) && return 0

  printf '\n'
  printf "Running %s upgrade(s)\n" "$count"

  # Execute hooks
  for script in "${scripts[@]}"; do
    local hook_name
    hook_name=$(basename "$script")
    if ! run_cmd --try "${CHILD_MARK} Running $hook_name" bash "$script"; then
      printf "${YEL}Upgrade %s failed (continuing update).${NC}\n" "$hook_name"
    fi
  done
}

read_update_meta() {
  local files=("$@")
  local -A component_set=()
  local reason_list=()
  local full_required=0

  for meta_file in "${files[@]}"; do
    local full_val
    local comps_val
    local reason_val
    full_val=$(json_get full "$meta_file" "")
    comps_val=$(json_get components "$meta_file" "")
    reason_val=$(json_get reason "$meta_file" "")

    if [[ "$full_val" == "true" ]]; then
      full_required=1
    fi

    if [[ -n "$comps_val" ]]; then
      IFS=',' read -ra _meta_secs <<< "$comps_val"
      for comp in "${_meta_secs[@]}"; do
        comp="${comp// /}"
        [[ -z "$comp" ]] && continue
        if ! validate_component "$comp"; then
          exit 1
        fi
        component_set["$comp"]=1
      done
    fi

    if [[ -n "$reason_val" ]]; then
      reason_list+=("$reason_val")
    fi
  done

  meta_full=0
  meta_components=""
  meta_reason=""

  if ((full_required==1)); then
    meta_full=1
  fi

  if (( ${#component_set[@]} > 0 )); then
    local comps=()
    for comp in "${!component_set[@]}"; do
      comps+=("$comp")
    done
    meta_components=$(printf '%s\n' "${comps[@]}" | sort | paste -sd, -)
  fi

  if (( ${#reason_list[@]} > 0 )); then
    for reason in "${reason_list[@]}"; do
      if [[ -n "$meta_reason" ]]; then
        meta_reason="${meta_reason}; ${reason}"
      else
        meta_reason="$reason"
      fi
    done
  fi
}

old_version=$(json_get git_ref "$VERSION_FILE" "")
if [[ -z "$old_version" ]]; then
  printf '\n'
  printf "${YEL}No version found in %s. Skipping upgrade hooks.${NC}\n" "$DATA_DIR/version.json"
elif [[ -z "$ref" ]]; then
  printf '\n'
  printf "${YEL}No target version specified. Skipping upgrade hooks.${NC}\n"
else
  upgrade_files=()
  while IFS= read -r script; do
    upgrade_files+=("$script")
  done < <(get_versioned_files "$old_version" "$ref" "$APP_DIR/scripts/upgrades" "sh")

  meta_files=()
  while IFS= read -r meta_file; do
    meta_files+=("$meta_file")
  done < <(get_versioned_files "$old_version" "$ref" "$APP_DIR/scripts/upgrades" "json")

  read_update_meta "${meta_files[@]}"
  run_upgrade_hooks "${upgrade_files[@]}"
fi

# Apply update metadata defaults
meta_forced_full=0
if ((meta_full==1)) && ((do_full==0)); then
  do_full=1
  do_all=0
  comps=""
  meta_forced_full=1
elif [[ -z "$comps" ]] && ((do_all==0)) && ((do_full==0)) && [[ -n "$meta_components" ]]; then
  comps="$meta_components"
fi

# Backup (recommended)
if ((do_backup==1)); then
  printf '\n'
  run_cmd "Creating backup" bash "$SCRIPT_DIR/backup.sh"
elif ((yes!=1)) && [[ -t 0 ]]; then
  printf '\n'
  read -r -p "Create a backup before updating? [Y/n]: " ans
  if [[ -z "${ans:-}" || "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    printf '\n'
    run_cmd "Creating backup" bash "$SCRIPT_DIR/backup.sh"
  fi
fi

# Full stack update helper (with downtime)
full_update() {
  printf '\n'
  printf "Full stack update\n"
  run_cmd "${CHILD_MARK} Building Docker images" "${COMPOSE_BASE[@]}" build
  run_cmd "${CHILD_MARK} Stopping stack" "${COMPOSE_BASE[@]}" down --remove-orphans
  run_cmd "${CHILD_MARK} Starting stack" "${COMPOSE_BASE[@]}" up -d --force-recreate --remove-orphans
  skip_components=1
  if ((migrate==1)); then
    printf '\n'
    printf "Applying migrations\n"
    run_cmd "${CHILD_MARK} Running database migrations" bash "$SCRIPT_DIR/db-migrate.sh"
  fi
}

append_component_if_missing() {
  local current="$1"
  local component="$2"

  if [[ -z "$current" ]]; then
    printf '%s' "$component"
    return
  fi
  if [[ ",$current," == *",$component,"* ]]; then
    printf '%s' "$current"
    return
  fi
  printf '%s,%s' "$current" "$component"
}

# Do not pull all images up-front; build/pull per-service below

# Option1: Full update (with downtime)
if ((do_full==1)); then
  if ((yes!=1)); then
    printf '\n'
    if ((meta_forced_full==1)); then
      printf "${YEL}Update metadata requires a full stack restart.${NC}\n"
    fi
    if [[ -n "$meta_reason" ]]; then
      printf "${YEL}Update reason: %s${NC}\n" "$meta_reason"
    fi
    printf "${YEL}This will stop ALL services, update, and restart the whole stack. Downtime WILL occur.${NC}\n"
    if [[ ! -t 0 ]]; then
      err "Non-interactive mode: pass --yes to proceed with full update"
      exit 1
    fi
    read -r -p "Proceed? [y/N]: " ans
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || { info "Aborted."; exit 1; }
  fi
  full_update
fi

# Option2: Components update
if ((do_full==0)); then
  if [[ -z "$comps" ]]; then
    if ((do_all==1)); then
      comps="app,worker-jobs,worker-monitor,traefik"
    elif [[ -n "$meta_components" ]]; then
      comps="$meta_components"
    else
      comps="$default_components"
    fi
  fi
  if [[ ",$comps," == *",app,"* ]]; then
    comps=$(append_component_if_missing "$comps" "worker-jobs")
    comps=$(append_component_if_missing "$comps" "worker-monitor")
    comps=$(append_component_if_missing "$comps" "traefik")
  fi

  if ((yes!=1)); then
    printf '\n'
    if [[ -n "$meta_reason" ]]; then
      printf "${YEL}Update reason: %s${NC}\n" "$meta_reason"
    fi
    printf "${YEL}This will update and restart components: %s${NC}\n" "${comps//,/ }"
    if [[ ! -t 0 ]]; then
      err "Non-interactive mode: pass --yes to proceed with component update"
      exit 1
    fi
    read -r -p "Proceed? [y/N]: " ans
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || { info "Aborted."; exit 1; }
  fi
fi

IFS=',' read -ra C <<< "$comps"

# Blue‑green helper (expects image already built/pulled as needed)
blue_green_rollout() {
  local service="$1"
  local timeout_s="${2:-300}"

  local old_ids
  old_ids=$(docker ps --filter "name=devpush-$service" --format '{{.ID}}' || true)
  local cur_cnt
  cur_cnt=$(printf '%s\n' "$old_ids" | wc -w)
  local target=$((cur_cnt+1)); [[ $target -lt 1 ]] && target=1
  
  run_cmd "${CHILD_MARK} Scaling up to $target container(s)" "${COMPOSE_BASE[@]}" up -d --scale "$service=$target" --no-recreate

  local new_id=""
  run_cmd "${CHILD_MARK} Detecting new container" bash -c '
    old_ids="'"$old_ids"'"
    for _ in $(seq 1 60); do
      cur_ids=$(docker ps --filter "name=devpush-'"$service"'" --format "{{.ID}}" | tr " " "\n" | sort)
      nid=$(comm -13 <(printf '%s\n' "$old_ids" | tr " " "\n" | sort) <(printf '%s\n' "$cur_ids"))
      if [[ -n "$nid" ]]; then
        printf "%s\n" "$nid"
        exit 0
      fi
      sleep 2
    done
    exit 1'
  new_id=$(docker ps --filter "name=devpush-$service" --format '{{.ID}}' | tr ' ' '\n' | sort | comm -13 <(printf '%s\n' "$old_ids" | tr ' ' '\n' | sort) -)
  [[ -n "$new_id" ]] || { err "Failed to detect new container for '$service'"; return 1; }
  printf "  ${DIM}${CHILD_MARK} Container ID: %s${NC}\n" "$new_id"

  run_cmd "${CHILD_MARK} Verifying new container health (timeout: ${timeout_s}s)" \
    wait_container_healthy "$new_id" "$timeout_s" "$service"
  
  if [[ -n "$old_ids" ]]; then
    mapfile -t OLD_CONTAINERS <<<"$old_ids"
    run_cmd --try "${CHILD_MARK} Retiring old container(s)" bash -c '
      set -Eeuo pipefail
      for id in "$@"; do
        [[ -z "$id" ]] && continue
        docker stop "$id" >/dev/null 2>&1 || printf "  Failed to stop container %s\n" "$id" >&2
        docker rm "$id" >/dev/null 2>&1 || printf "  Failed to remove container %s\n" "$id" >&2
      done
    ' retire "${OLD_CONTAINERS[@]}"
    for id in "${OLD_CONTAINERS[@]}"; do
      printf "  ${DIM}${CHILD_MARK} Container ID: %s${NC}\n" "$id"
    done
  fi
}

recreate_rollout() {
  local service="$1"
  local timeout_s="${2:-180}"

  normalize_service_scale "$service" 1
  run_cmd "${CHILD_MARK} Recreating container" \
    "${COMPOSE_BASE[@]}" up -d --no-deps --force-recreate --scale "${service}=1" "$service"

  local cid
  cid=$(running_service_container_ids "$service" | head -1)
  [[ -n "$cid" ]] || { err "No running container for '${service}' after recreate"; return 1; }
  printf "  ${DIM}${CHILD_MARK} Container ID: %s${NC}\n" "$cid"
  run_cmd "${CHILD_MARK} Verifying container health (timeout: ${timeout_s}s)" \
    wait_container_healthy "$cid" "$timeout_s" "$service"
}

# Build/pull then rollout per service
rollout_service(){
  local s="$1"; local mode="$2"; local timeout_s="${3:-180}"
  printf '\n'
  printf "Updating %s\n" "$s"
  case "$s" in
    app|worker-jobs|worker-monitor)
      run_cmd "${CHILD_MARK} Building image" "${COMPOSE_BASE[@]}" build "$s"
      case "$s" in
        worker-jobs)
          verify_service_imports worker-jobs \
            'uv run python -c "from workers.jobs import WorkerSettings"'
          ;;
        worker-monitor)
          verify_service_imports worker-monitor \
            'uv run python -c "import importlib; importlib.import_module('"'"'workers.monitor'"'"')"'
          ;;
        app)
          verify_service_imports app \
            'uv run python -c "from config import get_settings; from models import User; from routers import auth, project, security"'
          ;;
      esac
      ;;
  esac
  if [[ "$mode" == "blue_green" ]]; then
    blue_green_rollout "$s" "$timeout_s"
  else
    recreate_rollout "$s" "$timeout_s"
  fi
}

if ((skip_components==0)); then
  printf '\n'
  printf "Preparing rollouts\n"
  for _prep in worker-jobs worker-monitor; do
    normalize_service_scale "$_prep" 1
  done
fi

if ((skip_components==0)); then
  for s in "${C[@]}"; do
    case "$s" in
      app)
        rollout_service app blue_green
        ;;
      worker-jobs)
        rollout_service worker-jobs recreate 180
        ;;
      worker-monitor)
        rollout_service worker-monitor recreate 120
        ;;
      traefik|loki|redis|docker-proxy|pgsql|mariadb|phpmyadmin|alloy)
        rollout_service "$s" recreate
        ;;
      *) err "unknown component: $s"; exit 1 ;;
    esac
  done
fi

if ((skip_components==0)) && [[ "$comps" == *"app"* ]]; then
  printf '\n'
  run_cmd "Ensuring MariaDB services are running" "${COMPOSE_BASE[@]}" up -d mariadb phpmyadmin
fi

# Apply database migrations
if ((skip_components==0)) && [[ "$comps" == *"app"* ]] && ((migrate==1)); then
  printf '\n'
  printf "Applying migrations\n"
  run_cmd "${CHILD_MARK} Running database migrations" bash "$SCRIPT_DIR/db-migrate.sh"
fi

# Update install metadata (version.json)
commit=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" rev-parse --verify HEAD)
repo_url=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" remote get-url origin 2>/dev/null || true)
if [[ -z "$ref" ]]; then
  ref=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" describe --tags --exact-match 2>/dev/null || true)
  [[ -n "$ref" ]] || ref=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" describe --tags --abbrev=0 2>/dev/null || true)
  [[ -n "$ref" ]] || ref=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" rev-parse --short "$commit")
fi
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
install_id=$(json_get install_id "$VERSION_FILE" "")
if [[ -z "$install_id" ]]; then
  install_id=$(cat /proc/sys/kernel/random/uuid)
fi

json_upsert "$VERSION_FILE" install_id "$install_id" git_ref "$ref" git_commit "$commit" git_repo "$repo_url" updated_at "$ts"

# Send telemetry
if ((telemetry==1)); then
printf '\n'
  if ! run_cmd --try "Sending telemetry" send_telemetry update; then
    printf "  ${DIM}${CHILD_MARK} Telemetry failed (non-fatal). Continuing update.${NC}\n"
  fi
fi

# Success message
printf '\n'
printf "${GRN}Update complete (%s → %s). ✔${NC}\n" "${old_version:-unknown}" "$ref"
