#!/usr/bin/env bash
set -Eeuo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$LIB_DIR/.." && pwd)"

# Colors and formatting
if [[ -t 1 ]]; then
  RED="$(printf '\033[31m')"
  GRN="$(printf '\033[32m')"
  YEL="$(printf '\033[33m')"
  BLD="$(printf '\033[1m')"
  DIM="$(printf '\033[2m')"
  NC="$(printf '\033[0m')"
else
  RED=""; GRN=""; YEL=""; BLD=""; DIM=""; NC=""
fi

is_utf8(){ case "${LC_ALL:-${LANG:-}}" in *UTF-8*|*utf8*) return 0;; *) return 1;; esac; }
CHILD_MARK="-"
if [[ -t 1 ]] && is_utf8; then CHILD_MARK="└─"; fi

err(){ printf "%b\n" "${RED}Error:${NC} $*" >&2; }
ok(){ printf "%b\n" "${GRN}Success:${NC} $*"; }
info(){ printf "%s\n" "$*"; }

# Verbosity level
VERBOSE="${VERBOSE:-0}"

# Log files
CMD_LOG="${TMPDIR:-/tmp}/devpush-cmd.$$.log"

# Detect environment (production or development)
ENVIRONMENT="${DEVPUSH_ENV:-}"
if [[ -z "$ENVIRONMENT" ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then
    ENVIRONMENT="development"
  else
    ENVIRONMENT="production"
  fi
fi

# Application, data, log, and backup paths
if [[ "$ENVIRONMENT" == "production" ]]; then
  APP_DIR="${DEVPUSH_APP_DIR:-/opt/devpush}"
  DATA_DIR="${DEVPUSH_DATA_DIR:-/var/lib/devpush}"
  LOG_DIR="${DEVPUSH_LOG_DIR:-/var/log/devpush}"
  BACKUP_DIR="${DEVPUSH_BACKUP_DIR:-/var/backups/devpush}"
else
  APP_DIR="${DEVPUSH_APP_DIR:-$PROJECT_ROOT}"
  DATA_DIR="${DEVPUSH_DATA_DIR:-$APP_DIR/data}"
  LOG_DIR="${DEVPUSH_LOG_DIR:-$APP_DIR/logs}"
  BACKUP_DIR="${DEVPUSH_BACKUP_DIR:-$APP_DIR/backups}"
fi

# Environment file, version file
ENV_FILE="$DATA_DIR/.env"
VERSION_FILE="$DATA_DIR/version.json"

export ENVIRONMENT APP_DIR DATA_DIR ENV_FILE VERSION_FILE LOG_DIR BACKUP_DIR

# Spinner for long-running commands
spinner() {
  local pid="$1"
  local delay=0.1
  local frames='-|\/'
  local i=0
  { tput civis 2>/dev/null || printf "\033[?25l"; } 2>/dev/null
  while kill -0 "$pid" 2>/dev/null; do
    i=$(((i + 1) % 4))
    printf "\r%s [%c]\033[K" "${SPIN_PREFIX:-}" "${frames:$i:1}"
    sleep "$delay"
  done
  { tput cnorm 2>/dev/null || printf "\033[?25h"; } 2>/dev/null
}

# Run command with optional --try flag to return instead of exiting on failure
run_cmd() {
  local no_exit=0
  if [[ "${1:-}" == "--try" ]]; then
    no_exit=1
    shift
  fi
  local msg="$1"; shift
  local cmd=("$@")

  if ((VERBOSE == 1)); then
    printf "%s\n" "$msg"
    "${cmd[@]}"
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
      err "Failed running: ${cmd[*]}"
      if (( no_exit == 1 )); then
        return $exit_code
      else
        exit $exit_code
      fi
    else
      printf "%b\n" "${GRN}Done ✔${NC}"
      return 0
    fi
  else
    : >"$CMD_LOG"
    "${cmd[@]}" >"$CMD_LOG" 2>&1 &
    local pid=$!
    SPIN_PREFIX="$msg"
    spinner "$pid"
    printf "\r\033[K"
    local saved_trap saved_e
    saved_trap="$(trap -p ERR 2>/dev/null || echo '')"
    saved_e="$-"
    trap - ERR 2>/dev/null || true
    set +e
    wait "$pid"
    local exit_code=$?
    if [[ "$saved_e" == *e* ]]; then
      set -e
    else
      set +e
    fi
    if [[ -n "$saved_trap" ]]; then
      if ! eval "$saved_trap" 2>/dev/null; then
        err "Failed to restore ERR trap - error handling may be compromised"
      fi
    fi
    if [[ $exit_code -ne 0 ]]; then
      printf "%b\n" "$SPIN_PREFIX ${RED}✖${NC}"
      printf '\n'
      err "Failed. Command output:"
      if [[ -s "$CMD_LOG" ]]; then
        if [[ -n "${SCRIPT_ERR_LOG:-}" ]]; then
          sed "s/^/  ${DIM}/" "$CMD_LOG" | sed "s/$/${NC}/" | tee -a "$SCRIPT_ERR_LOG" >&2
        else
          sed "s/^/  ${DIM}/" "$CMD_LOG" | sed "s/$/${NC}/" >&2
        fi
      else
        if [[ -n "${SCRIPT_ERR_LOG:-}" ]]; then
          printf "%b\n" "  ${DIM}(no output captured)${NC}" | tee -a "$SCRIPT_ERR_LOG" >&2
        else
          printf "%b\n" "  ${DIM}(no output captured)${NC}" >&2
        fi
      fi
      printf '\n'
      rm -f "$CMD_LOG" 2>/dev/null || true
      if (( no_exit == 1 )); then
        return $exit_code
      else
        exit $exit_code
      fi
    else
      printf "%b\n" "$SPIN_PREFIX ${GRN}✔${NC}"
      rm -f "$CMD_LOG" 2>/dev/null || true
      return 0
    fi
  fi
}

# Read a value from .env-style file
read_env_value(){
  local env_file="$1"; local key="$2"
  [[ -f "$env_file" ]] || return 0
  awk -v k="$key" '
    /^[[:space:]]*#/ {next}
    {
      match($0, /^[[:space:]]*([^=[:space:]]+)[[:space:]]*=/)
      if (RSTART > 0) {
        key_part = substr($0, RSTART, RLENGTH)
        gsub(/^[[:space:]]+|[[:space:]]+$|[[:space:]]*=$/, "", key_part)
        if (key_part == k) {
          val = substr($0, RSTART + RLENGTH)
          sub(/^[[:space:]]+/, "", val)
          if (val ~ /^"/) {
            val = substr(val, 2)
            sub(/"$/, "", val)
          } else if (val ~ /^'\''/) {
            val = substr(val, 2)
            sub(/'\''$/, "", val)
          } else {
            sub(/[[:space:]]*#.*$/, "", val)
          }
          sub(/[[:space:]]+$/, "", val)
          print val
          exit
        }
      }
    }
  ' "$env_file"
}

# Ensure generated MariaDB credentials exist for upgraded installs.
ensure_mariadb_env() {
  local env_file="${1:-$ENV_FILE}"
  [[ -f "$env_file" ]] || return 0

  local mariadb_root_password
  mariadb_root_password="$(read_env_value "$env_file" MARIADB_ROOT_PASSWORD)"
  if [[ -n "$mariadb_root_password" ]]; then
    return 0
  fi

  mariadb_root_password="$(openssl rand -base64 24 | tr -d '\n')"
  printf '\nMARIADB_ROOT_PASSWORD="%s"\n' "$mariadb_root_password" >>"$env_file"
  chmod 0600 "$env_file" >/dev/null 2>&1 || true

  if [[ "$ENVIRONMENT" == "production" ]]; then
    local service_user
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$env_file" >/dev/null 2>&1 || true
  fi

  printf "  ${DIM}${CHILD_MARK} Added generated MARIADB_ROOT_PASSWORD to %s${NC}\n" "$env_file"
}

# Read the optional GitHub repo token from env or .env.
github_repo_token() {
  if [[ -n "${DEVPUSH_GITHUB_REPO_TOKEN:-}" ]]; then
    printf "%s\n" "$DEVPUSH_GITHUB_REPO_TOKEN"
    return 0
  fi

  if [[ -f "$ENV_FILE" ]]; then
    local token
    token="$(read_env_value "$ENV_FILE" GITHUB_REPO_TOKEN)"
    if [[ -n "$token" ]]; then
      printf "%s\n" "$token"
      return 0
    fi
  fi

  return 1
}

# Populate GIT_AUTH_ARGS for authenticated GitHub fetches without persisting credentials.
GIT_AUTH_ARGS=()
set_github_repo_auth_args() {
  local repo_url="${1:-}"
  GIT_AUTH_ARGS=()
  [[ "$repo_url" == https://github.com/* ]] || return 0

  local token header
  token="$(github_repo_token 2>/dev/null || true)"
  [[ -n "$token" ]] || return 0

  header="$(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"
  GIT_AUTH_ARGS=(-c "http.extraHeader=AUTHORIZATION: basic ${header}")
}

# Get a value from a JSON file
json_get() {
  local expr="$1"
  local file="$2"
  local default="${3-}"

  if [[ "${expr:0:1}" != "." && "$expr" != "@" ]]; then
    expr=".$expr"
  fi

  if [[ ! -f "$file" ]]; then
    if [[ $# -ge 3 ]]; then
      printf "%s\n" "$default"
      return 0
    fi
    return 1
  fi

  local value
  value=$(jq -e -r "$expr // empty" "$file" 2>/dev/null || true)
  if [[ -n "$value" ]]; then
    printf "%s\n" "$value"
    return 0
  fi

  if [[ $# -ge 3 ]]; then
    printf "%s\n" "$default"
    return 0
  fi

  return 1
}

# Update a JSON file
json_upsert() {
  local file="$1"
  shift

  if (( $# % 2 != 0 )); then
    err "json_upsert expects key/value pairs"
    return 1
  fi

  local exists=0
  if [[ -f "$file" ]]; then
    exists=1
  else
    local dir
    dir="$(dirname "$file")"
    if [[ ! -d "$dir" ]]; then
      mkdir -p -m 0750 "$dir" || {
        err "json_upsert: failed to create directory: $dir"
        return 1
      }
    fi
  fi

  local jq_args=()
  local filter='(. // {}) as $base | $base + {'
  local first=1

  while [[ $# -gt 0 ]]; do
    local key="$1"
    local value="$2"
    shift 2

    if (( first )); then
      first=0
    else
      filter+=", "
    fi

    local arg_type="--arg"
    local processed="$value"
    if [[ "$value" =~ ^@json:(.*)$ ]]; then
      arg_type="--argjson"
      processed="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^-?[0-9]+$ || "$value" == "true" || "$value" == "false" ]]; then
      arg_type="--argjson"
    fi

    jq_args+=("$arg_type" "$key" "$processed")
    filter+="\"${key}\": \$${key}"
  done

  filter+='}'

  local output
  if (( exists )); then
    output="$(jq -c "${jq_args[@]}" "$filter" "$file")" || {
      err "json_upsert: failed to update $file"
      return 1
    }
  else
    output="$(jq -c -n "${jq_args[@]}" "$filter")" || {
      err "json_upsert: failed to build JSON for $file"
      return 1
    }
  fi

  printf '%s' "$output" > "$file" || {
    err "json_upsert: failed writing to $file"
    return 1
  }
}

# Error trap used by init_script_logging
_script_err_trap() {
  local s=$?
  local name="${CURRENT_SCRIPT_NAME:-script}"
  err "${name} failed (exit $s)"
  printf "%b\n" "${RED}Last command: $BASH_COMMAND${NC}"
  printf "%b\n" "${RED}Error output:${NC}"
  if [[ -n "${SCRIPT_ERR_LOG:-}" && -f "$SCRIPT_ERR_LOG" ]]; then
    cat "$SCRIPT_ERR_LOG" 2>/dev/null || printf "No error details captured\n"
  else
    printf "No error details captured\n"
  fi
  if declare -f on_error_hook >/dev/null 2>&1; then
    on_error_hook "$s"
  fi
  exit $s
}

# Initialize script logging
init_script_logging() {
  local name="${1:-$(basename "$0" .sh)}"
  local log_dir="${LOG_DIR:-/var/log/devpush}"
  CURRENT_SCRIPT_NAME="$name"

  install -d -m 0750 "$log_dir" >/dev/null 2>&1 || true
  SCRIPT_ERR_LOG="$log_dir/${name}-error.log"
  ln -sfn "$SCRIPT_ERR_LOG" "$log_dir/${name}_error.log" >/dev/null 2>&1 || true
  exec 2> >(tee "$SCRIPT_ERR_LOG" >&2)
  trap '_script_err_trap' ERR
}

# Write registry files (catalog.json and overrides.json)
write_registry_files() {
  local src_dir="${APP_DIR}/registry"
  local dst_dir="${DATA_DIR}/registry"
  local owner_args=()

  install -d "$dst_dir"

  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    if [[ -n "${SERVICE_USER:-}" ]]; then
      owner_args=(-o "$SERVICE_USER" -g "$SERVICE_USER")
    elif [[ -n "${SERVICE_UID:-}" && -n "${SERVICE_GID:-}" ]]; then
      owner_args=(-o "$SERVICE_UID" -g "$SERVICE_GID")
    fi
  fi

  local src_catalog="$src_dir/catalog.json"
  local dst_catalog="$dst_dir/catalog.json"
  if [[ -f "$src_catalog" ]]; then
    if [[ ! -f "$dst_catalog" ]]; then
      if ((${#owner_args[@]})); then
        run_cmd "${CHILD_MARK} Copying catalog.json" install "${owner_args[@]}" -m 0640 "$src_catalog" "$dst_catalog"
      else
        run_cmd "${CHILD_MARK} Copying catalog.json" install -m 0640 "$src_catalog" "$dst_catalog"
      fi
    else
      local src_version dst_version dst_source newest
      src_version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$src_catalog" | head -n1)"
      dst_version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$dst_catalog" | head -n1)"
      dst_source="$(sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$dst_catalog" | head -n1)"
      if [[ "$dst_source" == "bundled" && -n "$src_version" ]]; then
        if [[ -z "$dst_version" ]]; then
          if ((${#owner_args[@]})); then
            run_cmd "${CHILD_MARK} Refreshing catalog.json" install "${owner_args[@]}" -m 0640 "$src_catalog" "$dst_catalog"
          else
            run_cmd "${CHILD_MARK} Refreshing catalog.json" install -m 0640 "$src_catalog" "$dst_catalog"
          fi
        else
          newest="$(printf '%s\n%s\n' "$dst_version" "$src_version" | sort -V | tail -n1)"
          if [[ "$newest" == "$src_version" && "$src_version" != "$dst_version" ]]; then
            if ((${#owner_args[@]})); then
              run_cmd "${CHILD_MARK} Refreshing catalog.json" install "${owner_args[@]}" -m 0640 "$src_catalog" "$dst_catalog"
            else
              run_cmd "${CHILD_MARK} Refreshing catalog.json" install -m 0640 "$src_catalog" "$dst_catalog"
            fi
          else
            printf "%s Copying catalog.json ${YEL}⊘${NC}\n" "${CHILD_MARK}"
            printf "  ${DIM}%s Local catalog is up to date, skipping${NC}\n" "${CHILD_MARK}"
          fi
        fi
      elif [[ ! -f "$dst_catalog" ]] || [[ "$dst_source" != "bundled" ]]; then
        if ((${#owner_args[@]})); then
          run_cmd "${CHILD_MARK} Refreshing catalog.json" install "${owner_args[@]}" -m 0640 "$src_catalog" "$dst_catalog"
        else
          run_cmd "${CHILD_MARK} Refreshing catalog.json" install -m 0640 "$src_catalog" "$dst_catalog"
        fi
      else
        printf "%s Copying catalog.json ${YEL}⊘${NC}\n" "${CHILD_MARK}"
        printf "  ${DIM}%s Local catalog is up to date, skipping${NC}\n" "${CHILD_MARK}"
      fi
    fi
  else
    printf "%s Copying catalog.json ${YEL}⊘${NC}\n" "${CHILD_MARK}"
    printf "  ${DIM}%s Missing source catalog at %s${NC}\n" "${CHILD_MARK}" "$src_catalog"
  fi

  if [[ -f "$src_dir/overrides.json" && ! -f "$dst_dir/overrides.json" ]]; then
    if ((${#owner_args[@]})); then
      run_cmd "${CHILD_MARK} Copying overrides.json" install "${owner_args[@]}" -m 0640 "$src_dir/overrides.json" "$dst_dir/overrides.json"
    else
      run_cmd "${CHILD_MARK} Copying overrides.json" install -m 0640 "$src_dir/overrides.json" "$dst_dir/overrides.json"
    fi
  else
    printf "%s Copying overrides.json ${YEL}⊘${NC}\n" "${CHILD_MARK}"
    printf "  ${DIM}%s File already exists or missing source, skipping${NC}\n" "${CHILD_MARK}"
  fi
}

# Require sufficient disk on / for Docker images, databases, and builds.
check_disk_space() {
  local min_free_gb="${1:-4}"
  local min_total_gb="${2:-15}"
  local total_kb avail_kb total_gb avail_gb

  total_kb="$(df -Pk / | awk 'NR==2 {print $2}')"
  avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
  total_gb=$((total_kb / 1024 / 1024))
  avail_gb=$((avail_kb / 1024 / 1024))

  if (( total_gb < min_total_gb )); then
    err "Root disk is only ${total_gb}GB (need at least ${min_total_gb}GB for Pushify)."
    err "Resize the VPS disk, then reinstall. A 5GB root volume is too small for Docker + PostgreSQL + MariaDB."
    exit 1
  fi

  if (( avail_gb < min_free_gb )); then
    err "Only ${avail_gb}GB free on / (need at least ${min_free_gb}GB)."
    err "Free space: docker system prune -af  (only if safe), or expand the disk."
    exit 1
  fi
}

# Print app container diagnostics when startup health checks fail.
dump_app_start_failure() {
  local container="${1:-}"
  err "Application container failed to become healthy."
  if [[ -z "$container" ]]; then
    container="$(docker ps -a --filter "label=com.docker.compose.project=devpush" --filter "label=com.docker.compose.service=app" -q | head -1 || true)"
  fi
  if [[ -n "$container" ]]; then
    docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} exit={{.State.ExitCode}} error={{.State.Error}}' "$container" 2>&1 \
      | sed 's/^/  /' >&2 || true
    printf "${YEL}Last app logs:${NC}\n" >&2
    docker logs --tail 200 "$container" 2>&1 | sed 's/^/  /' >&2 || true
  fi
  if ((${#COMPOSE_BASE[@]})); then
    printf "${YEL}Compose services:${NC}\n" >&2
    "${COMPOSE_BASE[@]}" ps 2>&1 | sed 's/^/  /' >&2 || true
  fi
  printf "${YEL}Disk usage:${NC}\n" >&2
  df -h / 2>&1 | sed 's/^/  /' >&2 || true
  if command -v docker >/dev/null 2>&1; then
    docker system df 2>&1 | sed 's/^/  /' >&2 || true
  fi
  printf "${YEL}Next checks:${NC}\n" >&2
  printf "  validate_env %s\n" "$ENV_FILE" >&2
  printf "  compose.sh logs app --tail 200\n" >&2
  printf "  compose.sh exec app uv run python -c \"import main\"\n" >&2
}

# Validate environment variables
validate_env(){
  local env_file="$1"

  [[ -f "$env_file" ]] || { err "Not found: $env_file"; exit 1; }

  ensure_phpmyadmin_env "$env_file"

  # Core environment variables
  local required=(
    APP_HOSTNAME
    DEPLOY_DOMAIN
    GITHUB_APP_ID
    GITHUB_APP_NAME
    GITHUB_APP_PRIVATE_KEY
    GITHUB_APP_WEBHOOK_SECRET
    GITHUB_APP_CLIENT_ID
    GITHUB_APP_CLIENT_SECRET
    SECRET_KEY
    ENCRYPTION_KEY
    POSTGRES_PASSWORD
    MARIADB_ROOT_PASSWORD
    SERVER_IP
  )

  local missing=()
  local key value

  for key in "${required[@]}"; do
    value="$(read_env_value "$env_file" "$key")"
    [[ -n "$value" ]] || missing+=("$key")
  done

  # Certificate challenge provider-specific environment variables
  if [[ "$ENVIRONMENT" == "production" ]]; then
    local email="${LE_EMAIL:-$(read_env_value "$env_file" LE_EMAIL)}"
    [[ -n "$email" ]] || missing+=("LE_EMAIL")

    local provider
    provider="$(get_cert_challenge_provider "$env_file")"

    case "$provider" in
      default)
        ;;
      cloudflare)
        [[ -n "$(read_env_value "$env_file" CF_DNS_API_TOKEN)" ]] || missing+=("CF_DNS_API_TOKEN")
        ;;
      route53)
        for key in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION; do
          [[ -n "$(read_env_value "$env_file" "$key")" ]] || missing+=("$key")
        done
        ;;
      gcloud)
        [[ -n "$(read_env_value "$env_file" GCE_PROJECT)" ]] || missing+=("GCE_PROJECT")
        [[ -f $DATA_DIR/gcloud-sa.json ]] || missing+=("gcloud-sa.json")
        ;;
      digitalocean)
        [[ -n "$(read_env_value "$env_file" DO_AUTH_TOKEN)" ]] || missing+=("DO_AUTH_TOKEN")
        ;;
      azure)
        for key in AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_SUBSCRIPTION_ID AZURE_TENANT_ID AZURE_RESOURCE_GROUP; do
          [[ -n "$(read_env_value "$env_file" "$key")" ]] || missing+=("$key")
        done
        ;;
      *)
        err "Unknown certificate challenge provider: $provider"
        exit 1
        ;;
    esac
  fi

  if ((${#missing[@]})); then
    local joined
    joined="$(printf "%s, " "${missing[@]}")"
    joined="${joined%, }"
    err "Missing values in $env_file: $joined"
    printf "%b\n" "${YEL}Edit ${env_file} before starting (APP_HOSTNAME, DEPLOY_DOMAIN, LE_EMAIL, GitHub App).${NC}" >&2
    exit 1
  fi

  local pem hostname
  pem="$(read_env_value "$env_file" GITHUB_APP_PRIVATE_KEY)"
  pem="${pem//\\n/$'\n'}"
  if [[ "$pem" != *"BEGIN"* ]] || [[ "$pem" != *"PRIVATE KEY"* ]] || [[ "$pem" != *"END"* ]]; then
    err "GITHUB_APP_PRIVATE_KEY does not look like a PEM private key."
    printf "%b\n" "${YEL}Use one quoted line with \\n between PEM lines (see README).${NC}" >&2
    exit 1
  fi

  for key in APP_HOSTNAME DEPLOY_DOMAIN; do
    hostname="$(read_env_value "$env_file" "$key")"
    if [[ "$hostname" == *"://"* ]] || [[ "$hostname" == *"/"* ]] || [[ "$hostname" == *" "* ]]; then
      err "$key must be a bare hostname (e.g. panel.example.com), not a URL or path."
      exit 1
    fi
  done
}

# Normalize shell scripts after clone (CRLF-safe) and ensure they are executable.
prepare_shell_scripts() {
  local root="${1:-$APP_DIR}"
  local patterns=(scripts docker)
  local dir

  if command -v dos2unix >/dev/null 2>&1; then
    for dir in "${patterns[@]}"; do
      [[ -d "$root/$dir" ]] || continue
      find "$root/$dir" -type f \( -name '*.sh' -o -name 'entrypoint.*' \) -print0 2>/dev/null \
        | xargs -0 -r dos2unix -q 2>/dev/null || true
    done
  fi

  find "$root/scripts" -type f -name '*.sh' -print0 2>/dev/null \
    | xargs -0 -r chmod 0755 2>/dev/null || true
}

# Discard local changes and sync to FETCH_HEAD (used by install/update).
git_sync_fetch_head() {
  local app_dir="$1" user="$2" ref="$3"
  shift 3
  local -a auth=("$@")
  local fetch_cmd="git"
  if ((${#auth[@]})); then
    fetch_cmd=(git "${auth[@]}")
  fi

  runuser -u "$user" -- git -C "$app_dir" reset --hard HEAD 2>/dev/null || true
  runuser -u "$user" -- git -C "$app_dir" clean -fd
  runuser -u "$user" -- "${fetch_cmd[@]}" -C "$app_dir" fetch --force --depth 1 origin "$ref"
  runuser -u "$user" -- git -C "$app_dir" reset --hard FETCH_HEAD
}

# Validation constants
VALID_CERT_CHALLENGE_PROVIDERS="default|cloudflare|route53|gcloud|digitalocean|azure"
VALID_COMPONENTS="app|worker-jobs|worker-monitor|alloy|traefik|loki|redis|docker-proxy|pgsql|mariadb|phpmyadmin"

# Resolve certificate challenge provider from env
get_cert_challenge_provider() {
  local env_file="${1:-$ENV_FILE}"
  local provider="${CERT_CHALLENGE_PROVIDER:-}"

  if [[ -z "$provider" ]]; then
    provider="$(read_env_value "$env_file" CERT_CHALLENGE_PROVIDER)"
  fi

  provider="${provider:-default}"
  if [[ ! "$provider" =~ ^(${VALID_CERT_CHALLENGE_PROVIDERS//|/|})$ ]]; then
    err "Invalid certificate challenge provider: $provider (must be one of: $VALID_CERT_CHALLENGE_PROVIDERS)"
    exit 1
  fi

  printf "%s\n" "$provider"
}

# Validate component
validate_component() {
  local comp="$1"
  if [[ ! "$comp" =~ ^(${VALID_COMPONENTS//|/|})$ ]]; then
    err "Invalid component: $comp (must be one of: $VALID_COMPONENTS)"
    return 1
  fi
  return 0
}

running_service_container_ids() {
  local service="$1"
  docker ps -q --filter "name=devpush-${service}-" --filter "status=running" 2>/dev/null | sort -u || true
}

# Remove stopped duplicates and scale a compose service to a fixed count.
normalize_service_scale() {
  local service="$1"
  local target="${2:-1}"
  [[ -n "${COMPOSE_BASE:-}" ]] || return 0

  local exited=""
  exited=$(docker ps -aq --filter "name=devpush-${service}-" --filter "status=exited" 2>/dev/null || true)
  if [[ -n "$exited" ]]; then
    run_cmd --try "${CHILD_MARK} Removing stopped ${service} container(s)" \
      docker rm -f $exited
  fi

  run_cmd --try "${CHILD_MARK} Normalizing ${service} scale to ${target}" \
    "${COMPOSE_BASE[@]}" up -d --scale "${service}=${target}" --remove-orphans "$service"
}

wait_container_healthy() {
  local cid="$1"
  local timeout_s="${2:-300}"
  local service="${3:-container}"

  local deadline=$(( $(date +%s) + timeout_s ))
  local st="starting"
  while :; do
    local health
    health=$(docker inspect "$cid" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    if [[ -n "$health" ]]; then
      st="$health"
      [[ "$st" == "healthy" ]] && return 0
    else
      st=$(docker inspect "$cid" --format '{{.State.Status}}' 2>/dev/null || printf "starting")
      [[ "$st" == "running" ]] && return 0
    fi
    if [[ "$st" == "exited" || "$st" == "dead" ]]; then
      break
    fi
    [[ $(date +%s) -ge $deadline ]] && break
    sleep 3
  done

  err "${service} failed health check (status: ${st})"
  printf "${YEL}Last logs from ${service}:${NC}\n" >&2
  docker logs --tail 60 "$cid" 2>&1 | sed 's/^/  /' >&2 || true
  return 1
}

verify_service_imports() {
  local service="$1"
  local cmd="$2"
  # Override service entrypoint (app/workers exec uvicorn/arq and ignore CMD).
  run_cmd "${CHILD_MARK} Verifying ${service} imports" \
    timeout 300 "${COMPOSE_BASE[@]}" run --rm -T --no-deps --entrypoint sh "$service" -c "timeout 240 $cmd"
}

# Service user (used for ownership + container UID/GID)
SERVICE_USER="${DEVPUSH_SERVICE_USER:-}"
SERVICE_UID="${SERVICE_UID:-}"
SERVICE_GID="${SERVICE_GID:-}"

# Determine default service user
default_service_user() {
  if [[ -n "$SERVICE_USER" ]]; then
    printf "%s\n" "$SERVICE_USER"
    return
  fi
  if [[ -n "${DEVPUSH_SERVICE_USER:-}" ]]; then
    printf "%s\n" "$DEVPUSH_SERVICE_USER"
    return
  fi

  if [[ "$ENVIRONMENT" == "production" ]]; then
    printf "devpush\n"
  else
    if command -v id >/dev/null 2>&1; then
      id -un
    else
      printf "%s\n" "${USER:-devpush}"
    fi
  fi
}

# Ensure service UID/GID are set
set_service_ids() {
  local candidate=""
  if [[ -n "${SERVICE_USER:-}" ]]; then
    candidate="$SERVICE_USER"
  elif [[ -n "${DEVPUSH_SERVICE_USER:-}" ]]; then
    candidate="$DEVPUSH_SERVICE_USER"
  elif [[ "$ENVIRONMENT" == "production" ]]; then
    candidate="devpush"
  else
    candidate="$(id -un)"
  fi

  local uid="${SERVICE_UID:-}" gid="${SERVICE_GID:-}"

  if [[ -z "$uid" || -z "$gid" ]]; then
    if id -u "$candidate" >/dev/null 2>&1; then
      uid="$(id -u "$candidate")"
      gid="$(id -g "$candidate")"
    else
      if [[ "$ENVIRONMENT" == "production" ]]; then
        err "Service user '$candidate' not found. Has install.sh been run?"
        exit 1
      fi
      candidate="$(id -un)"
      uid="$(id -u)"
      gid="$(id -g)"
    fi
  fi

  SERVICE_USER="$candidate"
  SERVICE_UID="$uid"
  SERVICE_GID="$gid"
  export SERVICE_USER SERVICE_UID SERVICE_GID
}

# Fix mixed root/service-user ownership (e.g. after manual git as root).
ensure_app_dir_ownership() {
  [[ "$ENVIRONMENT" == "production" ]] || return 0
  [[ -d "$APP_DIR" ]] || return 0
  [[ -n "${SERVICE_USER:-}" ]] || set_service_ids
  run_cmd "${CHILD_MARK} Ensuring app directory ownership" \
    chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
}

# Ensure traefik/acme.json exists with proper perms
ensure_acme_json() {
  install -d -m 0755 "$DATA_DIR/traefik" >/dev/null 2>&1 || true
  touch "$DATA_DIR/traefik/acme.json" >/dev/null 2>&1 || true
  chmod 600 "$DATA_DIR/traefik/acme.json" >/dev/null 2>&1 || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$DATA_DIR/traefik/acme.json" >/dev/null 2>&1 || true
  fi
}

# Traefik file provider: security middlewares and pushify-security service
ensure_security_middlewares_file() {
  install -d -m 0755 "$DATA_DIR/traefik" >/dev/null 2>&1 || true
  local file="$DATA_DIR/traefik/security-middlewares.yml"
  local ip_criterion cf_auth cf_mode want_cf
  if [[ -f "${APP_DIR}/scripts/provision/proxy-trust.sh" ]]; then
    # shellcheck source=provision/proxy-trust.sh
    source "${APP_DIR}/scripts/provision/proxy-trust.sh"
  fi
  ip_criterion="$(devpush_traefik_ip_criterion_yaml 2>/dev/null || printf '%s\n' '          ipStrategy:' '            depth: 1')"
  cf_auth=""
  if devpush_behind_cloudflare 2>/dev/null; then
    cf_auth="          - CF-Connecting-IP"
    cf_mode="cf"
  else
    cf_mode="direct"
  fi
  local want_https="1"
  devpush_https_enabled 2>/dev/null || want_https="0"
  if [[ -f "$file" ]] && grep -q 'pushify-ratelimit-v5e' "$file" 2>/dev/null \
    && grep -q 'pushify-edge-guard:' "$file" 2>/dev/null \
    && grep -q 'pushify-forward-auth:' "$file" 2>/dev/null \
    && ! grep -q 'pushify-challenge-redirect:' "$file" 2>/dev/null; then
    want_cf="direct"
    grep -q 'CF-Connecting-IP' "$file" 2>/dev/null && want_cf="cf"
    local has_redirect="0"
    grep -q 'pushify-redirect-https:' "$file" 2>/dev/null && has_redirect="1"
    if [[ "$want_https" == "$has_redirect" && "$want_cf" == "$cf_mode" ]]; then
      return 0
    fi
  fi
  local entrypoints_yaml redirect_yaml
  entrypoints_yaml="$(devpush_traefik_entrypoints_yaml 2>/dev/null || true)"
  redirect_yaml=""
  if devpush_https_enabled 2>/dev/null; then
    redirect_yaml="    pushify-redirect-https:
      redirectScheme:
        scheme: https
        permanent: true
"
  fi
  cat >"$file" <<YAML
${entrypoints_yaml}
# pushify-ratelimit-v5e (${cf_mode}) — burst 100+ for ~50 assets/page; average caps sustained flood
http:
  services:
    pushify-security:
      loadBalancer:
        servers:
          - url: http://app:8000
  middlewares:
${redirect_yaml}    pushify-edge-guard:
      chain:
        middlewares:
          - pushify-edge-total-inflight@file
          - pushify-edge-inflight@file
          - pushify-edge-ratelimit@file
    pushify-edge-total-inflight:
      inFlightReq:
        amount: 200
    pushify-edge-inflight:
      inFlightReq:
        amount: 50
        sourceCriterion:
${ip_criterion}
    pushify-edge-ratelimit:
      rateLimit:
        average: 35
        burst: 100
        period: 1s
        sourceCriterion:
${ip_criterion}
    pushify-inflight-panel:
      inFlightReq:
        amount: 30
        sourceCriterion:
${ip_criterion}
    pushify-inflight-deploy:
      inFlightReq:
        amount: 6
        sourceCriterion:
${ip_criterion}
    pushify-inflight-attack:
      inFlightReq:
        amount: 4
        sourceCriterion:
${ip_criterion}
    pushify-ratelimit-panel:
      rateLimit:
        average: 40
        burst: 80
        period: 1s
        sourceCriterion:
${ip_criterion}
    pushify-ratelimit-standard:
      rateLimit:
        average: 50
        burst: 120
        period: 1s
        sourceCriterion:
${ip_criterion}
    pushify-ratelimit-high:
      rateLimit:
        average: 30
        burst: 80
        period: 1s
        sourceCriterion:
${ip_criterion}
    pushify-ratelimit-attack:
      rateLimit:
        average: 15
        burst: 40
        period: 1s
        sourceCriterion:
${ip_criterion}
    pushify-forward-auth:
      forwardAuth:
        address: http://app:8000/security/forward-auth
        trustForwardHeader: true
        authResponseHeaders:
          - X-Pushify-Verified
        authRequestHeaders:
          - Cookie
          - User-Agent
          - X-Forwarded-Host
          - X-Forwarded-Uri
          - X-Forwarded-For
          - X-Forwarded-Proto
${cf_auth}
          - Accept
          - Accept-Language
          - Sec-Fetch-Dest
          - Sec-Fetch-Mode
          - Sec-Fetch-Site
    pushify-headers:
      headers:
        browserXssFilter: true
        contentTypeNosniff: true
        frameDeny: true
        referrerPolicy: strict-origin-when-cross-origin
        customResponseHeaders:
          Server: ""
          X-Powered-By: ""
    pushify-noindex:
      headers:
        customResponseHeaders:
          X-Robots-Tag: "noindex, nofollow, noarchive"
YAML
  chmod 0644 "$file" 2>/dev/null || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$file" 2>/dev/null || true
  fi
}

# Block https://SERVER_IP/ and raw-IP Host headers (see scripts/provision/origin-shield.sh)
ensure_origin_shield_file() {
  [[ "${ENVIRONMENT:-}" == "production" ]] || return 0
  [[ -f "${APP_DIR:-}/scripts/provision/origin-shield.sh" ]] || return 0
  bash "${APP_DIR}/scripts/provision/origin-shield.sh"
}

# Traefik JSON access log for CrowdSec (host bouncer reads DATA_DIR/traefik/logs/access.log)
ensure_traefik_log_dir() {
  install -d -m 0755 "$DATA_DIR/traefik/logs" >/dev/null 2>&1 || true
  touch "$DATA_DIR/traefik/logs/access.log" 2>/dev/null || true
  chmod 0644 "$DATA_DIR/traefik/logs/access.log" 2>/dev/null || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    service_user="$(default_service_user)"
    chown -R "$service_user:$service_user" "$DATA_DIR/traefik/logs" 2>/dev/null || true
  fi
}

# Apply Traefik label + file-provider changes (browser challenge, deploy error routes)
refresh_traefik_security() {
  ensure_acme_json
  ensure_security_middlewares_file
  ensure_origin_shield_file
  ensure_traefik_log_dir
  set_compose_base
  run_cmd "${CHILD_MARK} Reloading Traefik" "${COMPOSE_BASE[@]}" up -d --force-recreate --no-deps traefik
}

# Docker compose variables
COMPOSE_BIN=()
COMPOSE_ARGS=()
COMPOSE_ENV=()
COMPOSE_BASE=()

# Detect compose command (`docker compose` preferred)
set_compose_cmd() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN=(docker compose)
    return 0
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_BIN=(docker-compose)
    return 0
  fi
  return 1
}

# Populate COMPOSE_BASE for docker compose invocations
set_compose_base() {
  set_service_ids
  if ((${#COMPOSE_BIN[@]} == 0)); then
    if ! set_compose_cmd; then
      err "Neither 'docker compose' nor 'docker-compose' is available. Install Docker v20.10+ or docker-compose."
      exit 1
    fi
  fi

  local ssl="$(get_cert_challenge_provider)"
  COMPOSE_ARGS=(-p devpush -f "$APP_DIR/compose/base.yml")
  if [[ "$ENVIRONMENT" == "production" ]]; then
    if [[ -f "${APP_DIR}/scripts/provision/proxy-trust.sh" ]]; then
      # shellcheck source=provision/proxy-trust.sh
      source "${APP_DIR}/scripts/provision/proxy-trust.sh"
    fi
    if devpush_https_enabled 2>/dev/null; then
      COMPOSE_ARGS+=(-f "$APP_DIR/compose/override.yml")
    else
      COMPOSE_ARGS+=(-f "$APP_DIR/compose/override.http.yml")
    fi
    COMPOSE_ARGS+=(-f "$APP_DIR/compose/ssl-${ssl}.yml")
  else
    COMPOSE_ARGS+=(-f "$APP_DIR/compose/override.dev.yml")
  fi

  local profiles="${DEVPUSH_COMPOSE_PROFILES:-}"
  if [[ -z "$profiles" && -f "${ENV_FILE:-}" ]]; then
    profiles="$(read_env_value "$ENV_FILE" DEVPUSH_COMPOSE_PROFILES)"
  fi
  if [[ -n "$profiles" ]]; then
    export COMPOSE_PROFILES="$profiles"
  fi

  COMPOSE_BASE=("${COMPOSE_BIN[@]}")
  if [[ -f "$ENV_FILE" ]]; then
    COMPOSE_BASE+=(--env-file "$ENV_FILE")
  fi
  COMPOSE_BASE+=("${COMPOSE_ARGS[@]}")
}


# Check if any devpush containers are running
is_stack_running() {
  docker ps --filter "label=com.docker.compose.project=devpush" --format "{{.ID}}" 2>/dev/null | grep -q .
}

# Total system RAM in megabytes.
get_system_memory_mb() {
  local mem_kb bytes

  if [[ -r /proc/meminfo ]]; then
    mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    if [[ -n "$mem_kb" ]]; then
      printf '%s\n' $((mem_kb / 1024))
      return 0
    fi
  fi

  if command -v sysctl >/dev/null 2>&1; then
    bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    if [[ -n "$bytes" && "$bytes" =~ ^[0-9]+$ ]]; then
      printf '%s\n' $((bytes / 1024 / 1024))
      return 0
    fi
  fi

  if command -v free >/dev/null 2>&1; then
    free -m | awk '/^Mem:/ {print $2; exit}'
    return 0
  fi

  printf '4096\n'
}

# Logical CPU count.
get_system_cpu_count() {
  local cpus

  if command -v nproc >/dev/null 2>&1; then
    nproc
    return 0
  fi

  if [[ -r /proc/cpuinfo ]]; then
    cpus="$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || true)"
    if [[ -n "$cpus" && "$cpus" -gt 0 ]]; then
      printf '%s\n' "$cpus"
      return 0
    fi
  fi

  if command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.ncpu 2>/dev/null || printf '2\n'
    return 0
  fi

  printf '2\n'
}

# Compute recommended deployment resource limits from host capacity.
# Sets RECOMMENDED_* globals and DETECTED_SYSTEM_MEMORY_MB / DETECTED_SYSTEM_CPUS.
recommend_deployment_resources() {
  local total_mem total_cpus usable_cpus

  total_mem="$(get_system_memory_mb)"
  total_cpus="$(get_system_cpu_count)"
  (( total_mem < 512 )) && total_mem=512
  (( total_cpus < 1 )) && total_cpus=1
  usable_cpus=$((total_cpus > 1 ? total_cpus - 1 : 1))

  DETECTED_SYSTEM_MEMORY_MB="$total_mem"
  DETECTED_SYSTEM_CPUS="$total_cpus"

  # Hosts with <=6GB RAM: no deploy memory cap (builds need burst RAM; caps caused OOM on 4GB VPS).
  if (( total_mem <= 6144 )); then
    RECOMMENDED_DEFAULT_MEMORY_MB=0
    RECOMMENDED_MAX_MEMORY_MB=0
    if (( total_mem <= 2048 )); then
      RECOMMENDED_DEFAULT_CPUS="0.25"
      RECOMMENDED_MAX_CPUS="1.0"
    elif (( total_mem <= 4096 )); then
      RECOMMENDED_DEFAULT_CPUS="0.50"
      RECOMMENDED_MAX_CPUS="2.0"
    else
      RECOMMENDED_DEFAULT_CPUS="0.50"
      RECOMMENDED_MAX_CPUS="3.0"
    fi
    RECOMMENDED_RUNNER_FALLBACK_MEMORY_MB=0
    RECOMMENDED_RUNNER_FALLBACK_CPUS="$RECOMMENDED_DEFAULT_CPUS"
    RECOMMENDED_MAX_CPUS="$(
      awk -v max="$RECOMMENDED_MAX_CPUS" -v usable="$usable_cpus" \
        'BEGIN { printf "%.1f\n", (max > usable ? usable : max) }'
    )"
    RECOMMENDED_DEFAULT_CPUS="$(
      awk -v def="$RECOMMENDED_DEFAULT_CPUS" -v max="$RECOMMENDED_MAX_CPUS" \
        'BEGIN { printf "%.2f\n", (def > max ? max : def) }'
    )"
    return 0
  fi

  if (( total_mem <= 8192 )); then
    RECOMMENDED_DEFAULT_MEMORY_MB=3072
    RECOMMENDED_MAX_MEMORY_MB=6144
    RECOMMENDED_DEFAULT_CPUS="1.00"
    RECOMMENDED_MAX_CPUS="4.0"
  elif (( total_mem <= 16384 )); then
    RECOMMENDED_DEFAULT_MEMORY_MB=4096
    RECOMMENDED_MAX_MEMORY_MB=8192
    RECOMMENDED_DEFAULT_CPUS="1.00"
    RECOMMENDED_MAX_CPUS="6.0"
  else
    RECOMMENDED_DEFAULT_MEMORY_MB=6144
    RECOMMENDED_MAX_MEMORY_MB=16384
    RECOMMENDED_DEFAULT_CPUS="1.00"
    RECOMMENDED_MAX_CPUS="8.0"
  fi

  RECOMMENDED_MAX_CPUS="$(
    awk -v max="$RECOMMENDED_MAX_CPUS" -v usable="$usable_cpus" \
      'BEGIN { printf "%.1f\n", (max > usable ? usable : max) }'
  )"
  RECOMMENDED_DEFAULT_CPUS="$(
    awk -v def="$RECOMMENDED_DEFAULT_CPUS" -v max="$RECOMMENDED_MAX_CPUS" \
      'BEGIN { printf "%.2f\n", (def > max ? max : def) }'
  )"
  RECOMMENDED_RUNNER_FALLBACK_MEMORY_MB="$RECOMMENDED_DEFAULT_MEMORY_MB"
  RECOMMENDED_RUNNER_FALLBACK_CPUS="$RECOMMENDED_DEFAULT_CPUS"
}

# True when PHPMYADMIN_HOSTNAME would break Traefik/Let's Encrypt (e.g. "db." if APP_HOSTNAME was empty).
_phpmyadmin_hostname_invalid() {
  local host="$1"
  [[ -z "$host" || "$host" == "db" || "$host" == "db." ]] && return 0
  [[ "$host" != *.* ]] && return 0
  return 1
}

# Set or repair PHPMYADMIN_HOSTNAME from APP_HOSTNAME (upgraded / partial .env installs).
ensure_phpmyadmin_env() {
  local env_file="${1:-$ENV_FILE}"
  [[ -f "$env_file" ]] || return 0

  local app_hostname phpmyadmin_hostname target
  app_hostname="$(read_env_value "$env_file" APP_HOSTNAME)"
  phpmyadmin_hostname="$(read_env_value "$env_file" PHPMYADMIN_HOSTNAME)"

  if [[ -z "$app_hostname" ]]; then
    return 0
  fi

  target="db.${app_hostname}"
  if [[ -n "$phpmyadmin_hostname" ]] && ! _phpmyadmin_hostname_invalid "$phpmyadmin_hostname"; then
    return 0
  fi

  if grep -q '^PHPMYADMIN_HOSTNAME=' "$env_file" 2>/dev/null; then
    sed -i "s/^PHPMYADMIN_HOSTNAME=.*/PHPMYADMIN_HOSTNAME=${target}/" "$env_file"
  else
    printf '\nPHPMYADMIN_HOSTNAME=%s\n' "$target" >>"$env_file"
  fi
  chmod 0600 "$env_file" >/dev/null 2>&1 || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    local service_user
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$env_file" >/dev/null 2>&1 || true
  fi
  if _phpmyadmin_hostname_invalid "$phpmyadmin_hostname"; then
    printf "  ${DIM}${CHILD_MARK} Repaired PHPMYADMIN_HOSTNAME=%s${NC}\n" "$target"
  else
    printf "  ${DIM}${CHILD_MARK} Set PHPMYADMIN_HOSTNAME=%s${NC}\n" "$target"
  fi
}

# Append missing deployment resource keys to an env file (upgraded installs).
ensure_deployment_resource_env() {
  local env_file="${1:-$ENV_FILE}"
  [[ -f "$env_file" ]] || return 0

  recommend_deployment_resources

  local -a keys=(
    DEFAULT_CPUS
    MAX_CPUS
    DEFAULT_MEMORY_MB
    MAX_MEMORY_MB
    RUNNER_FALLBACK_CPUS
    RUNNER_FALLBACK_MEMORY_MB
  )
  local key value missing=0 added=0

  for key in "${keys[@]}"; do
    value="$(read_env_value "$env_file" "$key")"
    if [[ -z "$value" ]]; then
      missing=1
      break
    fi
  done
  (( missing == 0 )) && return 0

  {
    printf '\n# Deployment resources (auto-detected: %sMB RAM, %s CPUs)\n' \
      "$DETECTED_SYSTEM_MEMORY_MB" "$DETECTED_SYSTEM_CPUS"
    for key in "${keys[@]}"; do
      if [[ -z "$(read_env_value "$env_file" "$key")" ]]; then
        case "$key" in
          DEFAULT_CPUS) value="$RECOMMENDED_DEFAULT_CPUS" ;;
          MAX_CPUS) value="$RECOMMENDED_MAX_CPUS" ;;
          DEFAULT_MEMORY_MB) value="$RECOMMENDED_DEFAULT_MEMORY_MB" ;;
          MAX_MEMORY_MB) value="$RECOMMENDED_MAX_MEMORY_MB" ;;
          RUNNER_FALLBACK_CPUS) value="$RECOMMENDED_RUNNER_FALLBACK_CPUS" ;;
          RUNNER_FALLBACK_MEMORY_MB) value="$RECOMMENDED_RUNNER_FALLBACK_MEMORY_MB" ;;
        esac
        printf '%s=%s\n' "$key" "$value"
        added=1
      fi
    done
  } >>"$env_file"

  (( added == 0 )) && return 0

  chmod 0600 "$env_file" >/dev/null 2>&1 || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    local service_user
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$env_file" >/dev/null 2>&1 || true
  fi

  if (( RECOMMENDED_DEFAULT_MEMORY_MB > 0 )); then
    printf "  ${DIM}${CHILD_MARK} Set deployment limits from host (%sMB RAM, %s CPUs): default %sMB / %s CPU, max %sMB / %s CPU${NC}\n" \
      "$DETECTED_SYSTEM_MEMORY_MB" \
      "$DETECTED_SYSTEM_CPUS" \
      "$RECOMMENDED_DEFAULT_MEMORY_MB" \
      "$RECOMMENDED_DEFAULT_CPUS" \
      "$RECOMMENDED_MAX_MEMORY_MB" \
      "$RECOMMENDED_MAX_CPUS"
  else
    printf "  ${DIM}${CHILD_MARK} Set deployment CPU limits from host (%sMB RAM, %s CPUs): %s CPU max %s — deploy memory uncapped${NC}\n" \
      "$DETECTED_SYSTEM_MEMORY_MB" \
      "$DETECTED_SYSTEM_CPUS" \
      "$RECOMMENDED_DEFAULT_CPUS" \
      "$RECOMMENDED_MAX_CPUS"
  fi
}

# Update or append a single KEY=value in an env file.
upsert_env_value() {
  local env_file="$1" key="$2" value="$3"
  local tmp line="${key}=${value}"

  if [[ ! -f "$env_file" ]]; then
    printf '%s\n' "$line" >"$env_file"
    return 0
  fi

  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$env_file"; then
    tmp="$(mktemp)"
    awk -v k="$key" -v l="$line" '
      BEGIN { replaced = 0 }
      {
        if ($0 ~ "^[[:space:]]*" k "[[:space:]]*=") {
          if (!replaced) { print l; replaced = 1 }
        } else {
          print $0
        }
      }
      END { if (!replaced) print l }
    ' "$env_file" >"$tmp"
    mv "$tmp" "$env_file"
  else
    printf '%s\n' "$line" >>"$env_file"
  fi
}

# On small VPS hosts, remove deploy memory caps introduced by older installs.
sync_deployment_resources_for_host() {
  local env_file="${1:-$ENV_FILE}"
  [[ -f "$env_file" ]] || return 0

  recommend_deployment_resources
  (( DETECTED_SYSTEM_MEMORY_MB > 6144 )) && return 0

  local key val changed=0
  for key in DEFAULT_MEMORY_MB MAX_MEMORY_MB RUNNER_FALLBACK_MEMORY_MB; do
    val="$(read_env_value "$env_file" "$key")"
    if [[ -n "$val" && "$val" != "0" ]]; then
      upsert_env_value "$env_file" "$key" "0"
      changed=1
    fi
  done

  (( changed == 0 )) && return 0

  chmod 0600 "$env_file" >/dev/null 2>&1 || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    local service_user
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$env_file" >/dev/null 2>&1 || true
  fi

  printf "  ${DIM}${CHILD_MARK} Host has %sMB RAM: deploy memory limits disabled (use burst RAM for builds)${NC}\n" \
    "$DETECTED_SYSTEM_MEMORY_MB"
}

# Fetch public IP and persist unless --no-save
get_public_ip() {
  local ip=""

  # Try outbound services first
  local endpoint
  for endpoint in "https://api.ipify.org" "http://checkip.amazonaws.com"; do
    if command -v curl >/dev/null 2>&1; then
      ip="$(curl -fsS --max-time 3 "$endpoint" 2>/dev/null || true)"
    elif command -v wget >/dev/null 2>&1; then
      ip="$(wget -q -T 3 -O - "$endpoint" 2>/dev/null || true)"
    fi
    ip="${ip//$'\r'/}"
    [[ -n "$ip" ]] && break
  done

  # Fallback to local interface detection
  if [[ -z "$ip" ]]; then
    if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
      ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    elif [[ "$(uname -s)" == "Darwin" ]]; then
      ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || printf '')"
    fi
  fi

  printf "%s\n" "$ip"
}

# Send telemetry payload to API (or build one from version.json)
send_telemetry() {
  local event="$1"
  local payload="${2:-}"
  local endpoint="https://api.devpu.sh/v1/telemetry"

  if [[ -z "$payload" ]]; then
    [[ -f "$VERSION_FILE" ]] || return 0
    payload=$(jq -c --arg ev "$event" '. + {event: $ev}' "$VERSION_FILE" 2>/dev/null || printf '')
    [[ -n "$payload" ]] || return 0
  fi

  for attempt in 1 2 3; do
    if curl -fsSL -X POST -H 'Content-Type: application/json' -d "$payload" "$endpoint" >/tmp/devpush_telemetry.log 2>&1; then
      printf "Telemetry attempt %s succeeded.\n" "$attempt"
      rm -f /tmp/devpush_telemetry.log
      return 0
    fi
    cat /tmp/devpush_telemetry.log 2>/dev/null || true
    [[ $attempt -lt 3 ]] && sleep 1
  done

  return 1
}
