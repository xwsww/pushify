#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

[[ $EUID -eq 0 ]] || { printf "This script must be run as root (sudo).\n" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

init_script_logging "update"
on_error_hook() {
  if [[ -n "${current_commit:-}" ]]; then
    printf "To rollback: cd %s && git reset --hard %s\n" "$APP_DIR" "$current_commit"
  fi
}

usage(){
  cat <<USG
Usage: bash update.sh [--ref <tag>] [--all | --components <csv> | --full] [--no-migrate] [--no-security] [--no-telemetry] [--yes|-y] [--verbose]

Update /pushify/ by Git tag; performs rollouts (blue-green rollouts or simple restarts).
Run as: sudo bash scripts/update.sh (not sudo scripts/update.sh).

  --ref <tag>       Git tag to update to (default: latest stable tag)
                    Default update scope is app only unless overridden by flags or upgrade metadata
  --all             Update app, workers, and traefik (app,worker-jobs,worker-monitor,traefik)
  --components <csv>
                    Comma-separated list of services (${VALID_COMPONENTS//|/, })
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
ref=""; comps=""; do_all=0; do_full=0; do_backup=0; migrate=1; security=1; yes=0; telemetry=1
[[ "${NO_TELEMETRY:-0}" == "1" ]] && telemetry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) ref="$2"; shift 2 ;;
    --all) do_all=1; shift ;;
    --components)
      comps="$2"
      IFS=',' read -ra _up_secs <<< "$comps"
      for comp in "${_up_secs[@]}"; do
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

if ((do_all==1)) && [[ -n "$comps" ]]; then
  err "--all cannot be combined with --components"
  exit 1
fi
if ((do_full==1)) && { ((do_all==1)) || [[ -n "$comps" ]]; }; then
  err "--full cannot be combined with --all or --components"
  exit 1
fi

if [[ "$ENVIRONMENT" == "development" ]]; then
  err "This script is for production only. For development, simply pull code with git. More information: https://github.com/xwsww/pushify#development"
  exit 1
fi

cd "$APP_DIR" || { err "App dir not found: $APP_DIR"; exit 1; }

set_service_ids
ensure_app_dir_ownership

# Discard local changes before fetch (avoids checkout conflicts on scripts/*.sh).
if [[ -n "$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" status --porcelain 2>/dev/null)" ]]; then
  if ((yes==0)) && [[ -t 0 ]]; then
    printf "${YEL}Working directory has uncommitted changes.${NC}\n"
    read -r -p "Discard local changes and continue? [y/N] " ans
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]] || { printf "Aborted.\n"; exit 0; }
  elif ((yes==0)); then
    err "Working directory has uncommitted changes. Re-run with --yes to discard them."
    exit 1
  else
    printf "${YEL}Discarding local changes before update.${NC}\n"
  fi
  runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" reset --hard HEAD
  runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" clean -fd
fi

# Save current commit for rollback reference
current_commit=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)
current_version=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" describe --tags 2>/dev/null || printf "%s\n" "$current_commit")
repo_url=$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" remote get-url origin 2>/dev/null || true)
set_github_repo_auth_args "$repo_url"
git_fetch_cmd="git"
if (( ${#GIT_AUTH_ARGS[@]} )); then
  printf -v git_fetch_cmd 'git %q %q' "${GIT_AUTH_ARGS[0]}" "${GIT_AUTH_ARGS[1]}"
fi

# Resolve ref, fetch, then exec the updated apply script
printf '\n'
printf "Resolving update target\n"
if [[ -z "$ref" ]]; then
  run_cmd "${CHILD_MARK} Fetching tags" runuser -u "$SERVICE_USER" -- git "${GIT_AUTH_ARGS[@]}" -C "$APP_DIR" fetch --tags --force origin
  ref="$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" tag -l --sort=version:refname | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | tail -1 || true)"
  [[ -n "$ref" ]] || ref="$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" tag -l --sort=version:refname | tail -1 || true)"
  [[ -n "$ref" ]] || ref="main"
  printf "  ${DIM}${CHILD_MARK} Using latest stable tag: %s${NC}\n" "$ref"
else
  printf "  ${DIM}${CHILD_MARK} Using provided ref: %s${NC}\n" "$ref"
fi

# Fetch update
printf '\n'
printf "Fetching update\n"
if [[ "$ref" =~ ^(main|master|development)$ ]]; then
  run_cmd "${CHILD_MARK} Fetching branch: $ref" runuser -u "$SERVICE_USER" -- bash -c "cd \"$APP_DIR\" && ${git_fetch_cmd} fetch --force origin \"$ref\""
else
  run_cmd "${CHILD_MARK} Fetching ref: $ref" runuser -u "$SERVICE_USER" -- bash -c "cd \"$APP_DIR\" && ${git_fetch_cmd} fetch --force --depth 1 origin \"refs/tags/${ref}:refs/tags/${ref}\" || ${git_fetch_cmd} fetch --force --depth 1 origin \"${ref}\""
fi
run_cmd "${CHILD_MARK} Checking out" runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" reset --hard FETCH_HEAD
checked_out="$(runuser -u "$SERVICE_USER" -- git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || true)"
[[ -n "$checked_out" ]] && printf "  ${DIM}${CHILD_MARK} Checked out commit: %s${NC}\n" "$checked_out"
prepare_shell_scripts "$APP_DIR"

# Build args for update-apply
apply_args=()
if ((do_all==1)); then
  apply_args+=(--all)
elif [[ -n "$comps" ]]; then
  apply_args+=(--components "$comps")
elif ((do_full==1)); then
  apply_args+=(--full)
fi
if ((do_backup==1)); then
  apply_args+=(--backup)
fi
if ((migrate==0)); then
  apply_args+=(--no-migrate)
fi
if ((security==0)); then
  apply_args+=(--no-security)
fi
if ((telemetry==0)); then
  apply_args+=(--no-telemetry)
fi
if ((yes==1)); then
  apply_args+=(--yes)
fi
if [[ "${VERBOSE:-0}" == "1" ]]; then
  apply_args+=(--verbose)
fi

# Run update-apply script (allows us to update the script itself)
bash "$SCRIPT_DIR/update-apply.sh" --ref "$ref" "${apply_args[@]}"
