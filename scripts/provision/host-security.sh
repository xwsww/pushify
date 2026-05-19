#!/usr/bin/env bash
# Host hardening: sysctl + optional CrowdSec (production).
set -Eeuo pipefail
IFS=$'\n\t'

[[ $EUID -eq 0 ]] || { printf "host-security.sh must run as root.\n" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$SCRIPT_DIR/../lib.sh"

apply_sysctl_hardening() {
  local src="$APP_DIR/scripts/provision/sysctl-pushify.conf"
  local dst="/etc/sysctl.d/99-pushify.conf"
  [[ -f "$src" ]] || { err "Missing $src"; return 1; }
  if ! grep -q 'pushify-sysctl-v2' "$dst" 2>/dev/null; then
    install -m 0644 "$src" "$dst"
    modprobe nf_conntrack 2>/dev/null || true
    run_cmd "${CHILD_MARK} Applying sysctl" sysctl --system
  fi
}

install_host_security() {
  [[ "${ENVIRONMENT:-production}" == "production" ]] || return 0
  local enable="${DEVPUSH_ENABLE_CROWDSEC:-}"
  if [[ -z "$enable" && -f "${ENV_FILE:-}" ]]; then
    enable="$(read_env_value "$ENV_FILE" DEVPUSH_ENABLE_CROWDSEC 2>/dev/null || true)"
  fi
  enable="${enable:-1}"
  apply_sysctl_hardening
  if [[ "$enable" == "1" || "$enable" == "true" || "$enable" == "yes" ]]; then
    if command -v apt-get >/dev/null 2>&1; then
      if ! bash "$APP_DIR/scripts/provision/crowdsec.sh"; then
        printf "${YEL}%s CrowdSec install failed — install continues; re-run after git update:${NC}\n" "${CHILD_MARK}"
        printf "${DIM}  sudo bash %s/scripts/provision/host-security.sh${NC}\n" "$APP_DIR"
      fi
    else
      printf "${YEL}%s CrowdSec skipped (apt-based install only).${NC}\n" "${CHILD_MARK}"
    fi
  fi
  run_cmd "${CHILD_MARK} Ensuring Traefik security middlewares" ensure_security_middlewares_file
  if [[ -f "$APP_DIR/scripts/provision/origin-shield.sh" ]]; then
    bash "$APP_DIR/scripts/provision/origin-shield.sh" || \
      printf "${YEL}%s Origin shield failed (non-fatal); re-run: sudo bash %s/scripts/provision/origin-shield.sh${NC}\n" \
        "${CHILD_MARK}" "$APP_DIR"
  fi
}

install_host_security
