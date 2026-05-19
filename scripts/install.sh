#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

[[ $EUID -eq 0 ]] || { printf "This script must be run as root (sudo).\n" >&2; exit 1; }

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
if [[ -z "$SCRIPT_PATH" || "$SCRIPT_PATH" == "-" ]]; then
  SCRIPT_DIR="$(pwd)"
else
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
fi

# Resolve desired git repo/ref before loading lib.sh.
default_repo="https://github.com/xwsww/pushify.git"
repo="$default_repo"
ref=""
github_token="${DEVPUSH_GITHUB_REPO_TOKEN:-${GITHUB_REPO_TOKEN:-}}"
INSTALL_GIT_AUTH_ARGS=()
INSTALL_CURL_AUTH_ARGS=()

set_install_repo_auth_args() {
  local repo_url="${1:-}"
  INSTALL_GIT_AUTH_ARGS=()
  INSTALL_CURL_AUTH_ARGS=()
  [[ -n "$github_token" ]] || return 0

  case "$repo_url" in
    https://github.com/*)
      local header
      header="$(printf 'x-access-token:%s' "$github_token" | base64 | tr -d '\n')"
      INSTALL_GIT_AUTH_ARGS=(-c "http.extraHeader=AUTHORIZATION: basic ${header}")
      INSTALL_CURL_AUTH_ARGS=(-H "Authorization: Bearer ${github_token}")
      ;;
  esac
}

github_raw_base() {
  local repo_url="$1"
  local repo_path=""
  case "$repo_url" in
    https://github.com/*)
      repo_path="${repo_url#https://github.com/}"
      ;;
    git@github.com:*)
      repo_path="${repo_url#git@github.com:}"
      ;;
    *)
      return 1
      ;;
  esac

  repo_path="${repo_path%.git}"
  [[ -n "$repo_path" ]] || return 1
  printf "https://raw.githubusercontent.com/%s\n" "$repo_path"
}

args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  arg="${args[i]}"
  next="${args[i+1]:-}"
  if [[ "$arg" == "--repo" && -n "$next" ]]; then
    repo="$next"
    ((i+=1))
    continue
  fi
  if [[ "$arg" == "--ref" && -n "$next" ]]; then
    ref="$next"
    ((i+=1))
    continue
  fi
  if [[ "$arg" == "--github-token" && -n "$next" ]]; then
    github_token="$next"
    ((i+=1))
  fi
done

if [[ -z "$ref" ]]; then
  set_install_repo_auth_args "$repo"
  if ! refs_output="$(git "${INSTALL_GIT_AUTH_ARGS[@]}" ls-remote --tags --refs "$repo" 2>&1)"; then
    printf "Error: git ls-remote failed while resolving latest release (output below)\n%s\n" "$refs_output"
    exit 1
  fi
  ref="$(printf "%s\n" "$refs_output" | awk -F/ '{print $3}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"
  if [[ -z "$ref" ]]; then
    ref="$(printf "%s\n" "$refs_output" | awk -F/ '{print $3}' | sort -V | tail -1 || true)"
  fi
  if [[ -z "$ref" ]]; then
    ref="main"
  fi
fi

LIB_URL=""
if raw_base="$(github_raw_base "$repo" 2>/dev/null)"; then
  LIB_URL="${raw_base}/${ref}/scripts/lib.sh"
fi

# Load lib.sh: prefer local copy; otherwise fetch from the resolved ref
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/lib.sh" ]]; then
  source "$SCRIPT_DIR/lib.sh"
elif [[ -n "$LIB_URL" ]] && command -v curl >/dev/null 2>&1; then
  if ! curl -fsSL "${INSTALL_CURL_AUTH_ARGS[@]}" "$LIB_URL" -o /tmp/devpush_lib.sh; then
    printf "Error: Unable to load lib.sh from %s\n" "$LIB_URL" >&2
    exit 1
  fi
  source /tmp/devpush_lib.sh
else
  printf "Error: Unable to load lib.sh. Use a local checkout or a GitHub repo URL passed via --repo.\n" >&2
  exit 1
fi

# Logging
init_script_logging "install"

INSTALL_LOG_DIR="/tmp"
timestamp="$(date +%Y%m%d-%H%M%S)"
INSTALL_LOG="$INSTALL_LOG_DIR/devpush-install-${timestamp}.log"
mkdir -p "$INSTALL_LOG_DIR" || true
{
  printf "Install started: %s\n" "$(date -Iseconds)"
  printf "Effective ref: %s\n" "$ref"
} >"$INSTALL_LOG"
ln -sfn "$INSTALL_LOG" "$INSTALL_LOG_DIR/devpush-install.log"
exec > >(tee -a "$INSTALL_LOG") 2>&1

on_error_hook() {
  if [[ -n "${INSTALL_LOG:-}" ]]; then
    printf "%b\n" "${YEL}See install log for details: ${INSTALL_LOG}${NC}"
  fi
}

usage() {
  cat <<USG
Usage: install.sh [--repo <url>] [--ref <ref>] [--hostname <fqdn>] [--deploy-domain <fqdn>] [--le-email <email>] [--github-token <token>] [--yes] [--no-telemetry] [--verbose]

Install and configure /pushify/ on a server (Docker, user, repo, .env).

  --repo <url>           Git repo to clone (default: https://github.com/xwsww/pushify.git)
  --ref <ref>            Git ref (branch/tag/commit) to install (default: latest stable tag)
  --hostname <fqdn>      Panel hostname (APP_HOSTNAME), e.g. panel.example.com
  --deploy-domain <fqdn> Deployments wildcard root (DEPLOY_DOMAIN), e.g. deploy.example.com
  --le-email <email>     Let's Encrypt contact email (LE_EMAIL)
  --github-token <token> GitHub token for private repositories (also saved to data/.env)
  --yes, -y              Non-interactive, proceed without prompts
  --no-telemetry         Do not send telemetry
  -v, --verbose          Enable verbose output for debugging
  -h, --help             Show this help
USG
  exit 0
}

# Parse CLI flags
repo="$default_repo"
telemetry=1; yes_flag=0
install_hostname=""; install_deploy_domain=""; install_le_email=""
[[ "${NO_TELEMETRY:-0}" == "1" ]] && telemetry=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --ref) ref="$2"; shift 2 ;;
    --hostname) install_hostname="$2"; shift 2 ;;
    --deploy-domain) install_deploy_domain="$2"; shift 2 ;;
    --le-email) install_le_email="$2"; shift 2 ;;
    --github-token) github_token="$2"; shift 2 ;;
    --no-telemetry) telemetry=0; shift ;;
    --yes|-y) yes_flag=1; shift ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help) usage ;;
    *) err "Unknown option: $1"; usage; exit 1 ;;
  esac
done

set_install_repo_auth_args "$repo"

service_user="$(default_service_user)"

# Guard: prevent running in development mode
if [[ "$ENVIRONMENT" == "development" ]]; then
  err "This script is for production only. For development, install dependencies and start the stack (scripts/start.sh). More information: https://github.com/xwsww/pushify#development"
  exit 1
fi

# Warn if remnants of a prior install are present
existing_summary=()
if [[ -f "$DATA_DIR/version.json" ]]; then
  version_ref=$(sed -n 's/.*"git_ref":"\([^"]*\)".*/\1/p' "$DATA_DIR/version.json" | head -n1)
  existing_summary+=("version.json in $DATA_DIR (ref: ${version_ref:-unknown})")
fi
[[ -d "$APP_DIR/.git" ]] && existing_summary+=("repo at $APP_DIR")
[[ -f "$DATA_DIR/.env" ]] && existing_summary+=(".env in $DATA_DIR")

if ((${#existing_summary[@]})); then
  printf '\n'
  printf "Existing install detected:\n"
  for line in "${existing_summary[@]}"; do
    printf "  - %s\n" "$line"
  done
  if (( yes_flag == 0 )); then
    printf "Run sudo %s/scripts/uninstall.sh or re-run this installer with --yes to overwrite.\n" "$APP_DIR"
    exit 0
  fi
fi

# OS check (Debian/Ubuntu only)
. /etc/os-release || { err "Unsupported OS"; exit 1; }
case "${ID_LIKE:-$ID}" in
  *debian*|*ubuntu*) : ;;
  *) err "Only Ubuntu/Debian supported"; exit 1 ;;
esac
command -v apt-get >/dev/null || { err "apt-get not found"; exit 1; }

check_disk_space 3 15

# Detect system info for metadata
arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
distro_id="${ID:-unknown}"
distro_version="${VERSION_ID:-unknown}"

# Show summary of what we're installing
printf '\n'
printf "Installing /pushify/:\n"
printf "  - Repo: %s\n" "$repo"
printf "  - Ref/Version: %s\n" "$ref"
printf "  - OS: %s %s\n" "$distro_id" "$distro_version"
printf "  - Architecture: %s\n" "$arch"

# Banner
printf '\n'
printf "\033[38;5;51m    ██╗██████╗ ███████╗██╗   ██╗   ██╗██████╗ ██╗   ██╗███████╗██╗  ██╗\033[0m\n"
printf "\033[38;5;87m   ██╔╝██╔══██╗██╔════╝██║   ██║  ██╔╝██╔══██╗██║   ██║██╔════╝██║  ██║\033[0m\n"
printf "\033[38;5;123m  ██╔╝ ██║  ██║█████╗  ██║   ██║ ██╔╝ ██████╔╝██║   ██║███████╗███████║\033[0m\n"
printf "\033[38;5;159m ██╔╝  ██║  ██║██╔══╝  ╚██╗ ██╔╝██╔╝  ██╔═══╝ ██║   ██║╚════██║██╔══██║\033[0m\n"
printf "\033[38;5;195m██╔╝   ██████╔╝███████╗ ╚████╔╝██╔╝   ██║     ╚██████╔╝███████║██║  ██║\033[0m\n"
printf "\033[38;5;225m╚═╝    ╚═════╝ ╚══════╝  ╚═══╝ ╚═╝    ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═╝\033[0m\n"

# Ensure apt is fully non-interactive and avoid needrestart prompts
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
command -v curl >/dev/null || (apt-get update -yq && apt-get install -yq curl >/dev/null)

# Installs packages using apt-get
apt_install() {
  local pkgs=("$@"); local i
  for i in {1..5}; do
    if apt-get update -yq && apt-get install -yq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "${pkgs[@]}"; then return 0; fi
    sleep 3
  done
  return 1
}

# Installs Docker packages (apt-get first, then fallback to containerd.io version)
install_docker_packages() {
  if apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
    return 0
  fi

  printf "  ${DIM}${CHILD_MARK} Docker install failed; starting containerd.io fallback.${NC}\n" >&2

  rm -rf /var/lib/apt/lists/* || true
  if apt-get update -yq >/dev/null 2>&1; then
    printf "  ${DIM}${CHILD_MARK} Apt lists refreshed.${NC}\n"
  else
    printf "  ${DIM}${CHILD_MARK} Apt list refresh failed (continuing).${NC}\n" >&2
  fi

  # Get all available containerd.io versions, newest first (madison output is usually already newest-first,
  # but we don't assume; we just iterate in listed order).
  mapfile -t versions < <(apt-cache madison containerd.io | awk '{print $3}' | sed '/^$/d')

  if [[ "${#versions[@]}" -eq 0 ]]; then
    printf "  ${DIM}${CHILD_MARK} No containerd.io versions found via apt-cache madison.${NC}\n" >&2
    return 1
  fi

  for v in "${versions[@]}"; do
    if apt_install "containerd.io=$v" docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin; then
      printf "  ${DIM}${CHILD_MARK} containerd.io=%s succeeded.${NC}\n" "$v"
      return 0
    fi
    printf "  ${DIM}${CHILD_MARK} containerd.io=%s failed.${NC}\n" "$v" >&2
  done

  printf "  ${DIM}${CHILD_MARK} All containerd.io fallback versions failed.${NC}\n" >&2
  return 1
}

# Adds the Docker repository to the system
add_docker_repo() {
    install -m 0755 -d /etc/apt/keyrings
    case "${ID}" in
      ubuntu)
        gpg_url="https://download.docker.com/linux/ubuntu/gpg"
        codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
        repo_url="https://download.docker.com/linux/ubuntu"
        ;;
      debian|raspbian)
        gpg_url="https://download.docker.com/linux/debian/gpg"
        codename="${VERSION_CODENAME}"
        repo_url="https://download.docker.com/linux/debian"
        ;;
      *)
        if [[ "${ID_LIKE:-}" == *ubuntu* ]]; then
          gpg_url="https://download.docker.com/linux/ubuntu/gpg"
          codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
          repo_url="https://download.docker.com/linux/ubuntu"
        elif [[ "${ID_LIKE:-}" == *debian* ]]; then
          gpg_url="https://download.docker.com/linux/debian/gpg"
          codename="${VERSION_CODENAME}"
          repo_url="https://download.docker.com/linux/debian"
        else
          err "Unsupported distro for Docker repo: ID=${ID} ID_LIKE=${ID_LIKE:-}"
          exit 1
        fi
        ;;
    esac
    curl -fsSL "$gpg_url" | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    printf "deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] %s %s stable\n" "$arch" "$repo_url" "$codename" >/etc/apt/sources.list.d/docker.list
}

# Creates the system user
create_user() {
  if getent passwd "$service_user" >/dev/null 2>&1; then
    return 0
  fi
  useradd --system --user-group --home "$DATA_DIR" --shell /usr/sbin/nologin --no-create-home "$service_user"
}

# Records the install metadata
record_version() {
  local commit ts install_id repo_url
  commit=$(runuser -u "$service_user" -- git -C "$APP_DIR" rev-parse --verify HEAD)
  repo_url=$(runuser -u "$service_user" -- git -C "$APP_DIR" remote get-url origin 2>/dev/null || printf "%s\n" "$repo")
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  install_id=$(json_get install_id "$VERSION_FILE" "")
  if [[ -z "$install_id" ]]; then
    install_id=$(cat /proc/sys/kernel/random/uuid)
  fi
  json_upsert "$VERSION_FILE" install_id "$install_id" git_ref "$ref" git_commit "$commit" git_repo "$repo_url" updated_at "$ts" arch "$arch" distro "$distro_id" distro_version "$distro_version"
  chown "$service_user:$service_user" "$VERSION_FILE" || true
  chmod 0644 "$VERSION_FILE" || true
}

# Install base packages
printf '\n'
run_cmd "Installing base packages" apt_install ca-certificates git jq curl gnupg dos2unix

# Install Docker
printf '\n'
printf "Installing Docker\n"
run_cmd "${CHILD_MARK} Adding Docker repository" add_docker_repo
run_cmd "${CHILD_MARK} Installing Docker packages" install_docker_packages

# Ensure Docker service is running
run_cmd "${CHILD_MARK} Enabling Docker service" systemctl enable --now docker
run_cmd "${CHILD_MARK} Waiting for Docker daemon" bash -lc 'for i in $(seq 1 30); do docker info >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'

# Create user
if ! id -u "$service_user" >/dev/null 2>&1; then
  printf '\n'
  run_cmd "Creating system user (${service_user})" create_user
else
  printf '\n'
  printf "Creating system user '%s' ${YEL}⊘${NC}\n" "$service_user"
  printf "${DIM}%s User already exists${NC}\n" "${CHILD_MARK}"
fi
printf '\n'
printf "Setting up data\n"

# Create data directory
run_cmd "${CHILD_MARK} Creating data directory ($DATA_DIR)" install -o "$service_user" -g "$service_user" -m 0750 -d \
  "$DATA_DIR" "$DATA_DIR/traefik" "$DATA_DIR/traefik/logs" "$DATA_DIR/upload" "$DATA_DIR/registry"

# Generate .env file with secrets
generate_env() {
  local secret_key encryption_key postgres_password mariadb_root_password postgres_storage_password server_ip
  secret_key=$(openssl rand -hex 32)
  encryption_key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')
  postgres_password=$(openssl rand -base64 24 | tr -d '\n')
  mariadb_root_password=$(openssl rand -base64 24 | tr -d '\n')
  postgres_storage_password=$(openssl rand -base64 24 | tr -d '\n')
  server_ip=$(get_public_ip 2>/dev/null || printf "127.0.0.1")
  recommend_deployment_resources
  if (( RECOMMENDED_DEFAULT_MEMORY_MB > 0 )); then
    printf "%s Detected %sMB RAM, %s CPUs → deploy default %sMB / %s CPU (max %sMB / %s CPU)\n" \
      "$CHILD_MARK" \
      "$DETECTED_SYSTEM_MEMORY_MB" \
      "$DETECTED_SYSTEM_CPUS" \
      "$RECOMMENDED_DEFAULT_MEMORY_MB" \
      "$RECOMMENDED_DEFAULT_CPUS" \
      "$RECOMMENDED_MAX_MEMORY_MB" \
      "$RECOMMENDED_MAX_CPUS"
  else
    printf "%s Detected %sMB RAM, %s CPUs → deploy memory uncapped, CPU %s (max %s)\n" \
      "$CHILD_MARK" \
      "$DETECTED_SYSTEM_MEMORY_MB" \
      "$DETECTED_SYSTEM_CPUS" \
      "$RECOMMENDED_DEFAULT_CPUS" \
      "$RECOMMENDED_MAX_CPUS"
  fi

  cat > "$ENV_FILE" <<EOF
# Auto-generated by install.sh
# Secrets (auto-generated by install.sh)
SECRET_KEY="${secret_key}"
ENCRYPTION_KEY="${encryption_key}"
POSTGRES_PASSWORD="${postgres_password}"
MARIADB_ROOT_PASSWORD="${mariadb_root_password}"
POSTGRES_STORAGE_PASSWORD="${postgres_storage_password}"

# System (auto-generated by install.sh)
SERVER_IP="${server_ip}"

# Domains & SSL
APP_HOSTNAME="${install_hostname}"
DEPLOY_DOMAIN="${install_deploy_domain}"
LE_EMAIL="${install_le_email}"
PHPMYADMIN_HOSTNAME=db.${install_hostname}
ADMINER_HOSTNAME=pg.${install_hostname}
# default|cloudflare|route53|gcloud|digitalocean|azure
CERT_CHALLENGE_PROVIDER=default
# CF_DNS_API_TOKEN=

# GitHub App (see https://github.com/xwsww/pushify#quickstart)
GITHUB_APP_ID=
GITHUB_APP_NAME=
GITHUB_APP_PRIVATE_KEY=
GITHUB_APP_WEBHOOK_SECRET=
GITHUB_APP_CLIENT_ID=
GITHUB_APP_CLIENT_SECRET=

# Optional (legacy; not used for outbound mail)
# EMAIL_SENDER_ADDRESS=
GITHUB_REPO_TOKEN="${github_token}"
REGISTRY_CATALOG_URL=https://raw.githubusercontent.com/xwsww/pushify/refs/heads/main/registry/catalog.json
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
# MARIADB_HOST=mariadb
# MARIADB_PORT=3306
# MARIADB_ROOT_USER=root
# APP_NAME=/pushify/
# APP_DESCRIPTION=
# POSTGRES_DB=devpush
# POSTGRES_USER=devpush-app
# REDIS_URL=redis://redis:6379
# Deployment resources (auto-detected: ${DETECTED_SYSTEM_MEMORY_MB}MB RAM, ${DETECTED_SYSTEM_CPUS} CPUs)
DEFAULT_CPUS=${RECOMMENDED_DEFAULT_CPUS}
MAX_CPUS=${RECOMMENDED_MAX_CPUS}
DEFAULT_MEMORY_MB=${RECOMMENDED_DEFAULT_MEMORY_MB}
MAX_MEMORY_MB=${RECOMMENDED_MAX_MEMORY_MB}
RUNNER_FALLBACK_CPUS=${RECOMMENDED_RUNNER_FALLBACK_CPUS}
RUNNER_FALLBACK_MEMORY_MB=${RECOMMENDED_RUNNER_FALLBACK_MEMORY_MB}
# JOB_TIMEOUT_SECONDS=320
# DEPLOYMENT_TIMEOUT_SECONDS=300
# LOG_LEVEL=WARNING
# Host L3/L4: CrowdSec + ipset/iptables bouncer (set 0 to disable)
DEVPUSH_ENABLE_CROWDSEC=1
# Reject HTTP(S) when Host is SERVER_IP; UFW allow only 22/80/443 (set 0 to disable)
DEVPUSH_ORIGIN_SHIELD=1
DEVPUSH_UFW=1
# Optional: restrict SSH (port 22) to your IP, e.g. 203.0.113.10/32
# DEVPUSH_SSH_ALLOW_CIDR=
# Panel HTTPS (Traefik TLS + HTTP→HTTPS redirect). Set 0 for HTTP-only until LE certs exist.
DEVPUSH_ENABLE_HTTPS=1
# Cloudflare proxy: set 1 when orange-cloud DNS points to this server (off = direct, default)
DEVPUSH_BEHIND_CLOUDFLARE=0
# Optional: extra CDN/proxy CIDRs (comma-separated), or literal "cloudflare" for CF ranges only
# DEVPUSH_TRUSTED_PROXY_CIDRS=
EOF
  chown "$service_user:$service_user" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
}

if [[ ! -f "$ENV_FILE" ]]; then
  run_cmd "${CHILD_MARK} Generating .env file" generate_env
else
  printf "%s Generating .env file ${YEL}⊘${NC}\n" "${CHILD_MARK}"
  printf "${DIM}%s File already exists, skipping${NC}\n" "${CHILD_MARK}"
fi

# Add log dir (production)
if [[ "$ENVIRONMENT" == "production" ]]; then
  printf '\n'
  run_cmd "Creating log directory ($LOG_DIR)" install -o "$service_user" -g "$service_user" -m 0750 -d "$LOG_DIR"
fi

printf '\n'
printf "Setting up app\n"

run_cmd "${CHILD_MARK} Creating app directory" install -d -m 0755 "$APP_DIR"
run_cmd "${CHILD_MARK} Setting app directory ownership" chown -R "$service_user:$service_user" "$APP_DIR"

# Get code from GitHub
git_fetch_cmd="git"
if (( ${#INSTALL_GIT_AUTH_ARGS[@]} )); then
  printf -v git_fetch_cmd 'git %q %q' "${INSTALL_GIT_AUTH_ARGS[0]}" "${INSTALL_GIT_AUTH_ARGS[1]}"
fi
if [[ -d "$APP_DIR/.git" ]]; then
  # Repo exists, just fetch
  cmd_block="
    set -ex
    cd '$APP_DIR'
    git remote get-url origin >/dev/null 2>&1 || git remote add origin '$repo'
    ${git_fetch_cmd} fetch --depth 1 origin '$ref'
  "
  run_cmd "${CHILD_MARK} Fetching repo updates ($repo)" runuser -u "$service_user" -- bash -c "$cmd_block"
else
  # New clone
  cmd_block="
    set -ex
    cd '$APP_DIR'
    git init
    git remote add origin '$repo'
    ${git_fetch_cmd} fetch --depth 1 origin '$ref'
  "
  run_cmd "${CHILD_MARK} Cloning repo ($repo)" runuser -u "$service_user" -- bash -c "$cmd_block"
fi

run_cmd "${CHILD_MARK} Checking out ref ($ref)" runuser -u "$service_user" -- git -C "$APP_DIR" reset --hard FETCH_HEAD
runuser -u "$service_user" -- git -C "$APP_DIR" config core.autocrlf false

cd "$APP_DIR"
prepare_shell_scripts "$APP_DIR"

if [[ "$ENVIRONMENT" == "production" && -f "$APP_DIR/scripts/provision/host-security.sh" ]]; then
  printf '\n'
  run_cmd "${CHILD_MARK} Applying host security (sysctl, CrowdSec)" \
    bash "$APP_DIR/scripts/provision/host-security.sh"
fi

# Create registry files (catalog.json and overrides.json)
printf '\n'
printf "Creating registry files\n"
write_registry_files

# Save install metadata (version.json)
printf '\n'
run_cmd "Saving install metadata (${VERSION_FILE})" record_version

# Send telemetry
if ((telemetry==1)); then
  printf '\n'
  if ! run_cmd --try "Sending telemetry" send_telemetry install; then
    printf "  ${DIM}${CHILD_MARK} Telemetry failed (non-fatal). Continuing install.${NC}\n"
  fi
fi

printf '\n'
printf "Installing systemd unit\n"

# Install systemd unit
unit_path="/etc/systemd/system/devpush.service"
run_cmd "${CHILD_MARK} Installing unit file" install -m 0644 "$APP_DIR/scripts/devpush.service" "$unit_path"
run_cmd "${CHILD_MARK} Installing pushify.service alias" ln -sf devpush.service /etc/systemd/system/pushify.service
run_cmd "${CHILD_MARK} Reloading systemd" systemctl daemon-reload
run_cmd "${CHILD_MARK} Enabling devpush.service" systemctl enable devpush.service

# Success message
printf '\n'
printf "${GRN}Install complete (version: %s). ✔${NC}\n" "$ref"
printf '\n'
printf "Next steps:\n"
printf "  1. Create a GitHub App: https://github.com/xwsww/pushify#quickstart\n"
printf "  2. Edit .env (required before first start): sudo nano %s\n" "$ENV_FILE"
printf "     Set APP_HOSTNAME, DEPLOY_DOMAIN, LE_EMAIL, and GitHub App credentials.\n"
printf "  3. DNS: A record for panel host + A record for *.DEPLOY_DOMAIN → %s\n" "$(read_env_value "$ENV_FILE" SERVER_IP 2>/dev/null || get_public_ip 2>/dev/null || echo 'SERVER_IP')"
printf "  4. Validate: sudo bash -c 'source %s/scripts/lib.sh; validate_env %s'\n" "$APP_DIR" "$ENV_FILE"
printf "  5. Start only after validate_env succeeds:\n"
printf "     sudo systemctl start devpush.service  (alias: pushify.service)\n"
printf "     Updates later: sudo bash %s/scripts/update.sh -y\n" "$APP_DIR"
printf "     After git pull only: sudo bash %s/scripts/reload-app.sh\n" "$APP_DIR"
printf "     First start builds images and may take 10–15 minutes.\n"
printf "     Do not start before step 4 — an empty .env will fail immediately.\n"
printf "  6. Disk: root volume needs ≥15GB total and ≥4GB free (docker system df).\n"
printf "     Optional logs/DB UI: add DEVPUSH_COMPOSE_PROFILES=optional to .env\n"
printf "  7. Host security: sysctl + CrowdSec + origin shield (block access by raw IP Host)\n"
printf "     Disable CrowdSec: DEVPUSH_ENABLE_CROWDSEC=0 | shield/UFW: DEVPUSH_ORIGIN_SHIELD=0 DEVPUSH_UFW=0\n"
printf "     Cloudflare (orange cloud): set DEVPUSH_BEHIND_CLOUDFLARE=1 in .env, then re-run host-security.sh\n"
printf "     Re-apply: sudo bash %s/scripts/provision/host-security.sh\n" "$APP_DIR"
printf '\n'
printf "Repo & install notes: https://github.com/xwsww/pushify#readme\n"
