#!/usr/bin/env bash
# Origin shield: block HTTP(S) by raw IP Host, optional UFW, optional CDN trusted proxy CIDRs.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$SCRIPT_DIR/../lib.sh"
# shellcheck source=proxy-trust.sh
source "$SCRIPT_DIR/proxy-trust.sh"

origin_shield_enabled() {
  local enable="${DEVPUSH_ORIGIN_SHIELD:-}"
  if [[ -z "$enable" && -f "${ENV_FILE:-}" ]]; then
    enable="$(read_env_value "$ENV_FILE" DEVPUSH_ORIGIN_SHIELD 2>/dev/null || true)"
  fi
  enable="${enable:-1}"
  [[ "$enable" == "1" || "$enable" == "true" || "$enable" == "yes" ]]
}

origin_shield_ufw_enabled() {
  local enable="${DEVPUSH_UFW:-}"
  if [[ -z "$enable" && -f "${ENV_FILE:-}" ]]; then
    enable="$(read_env_value "$ENV_FILE" DEVPUSH_UFW 2>/dev/null || true)"
  fi
  enable="${enable:-1}"
  [[ "$enable" == "1" || "$enable" == "true" || "$enable" == "yes" ]]
}

write_traefik_origin_shield() {
  local server_ip cidrs_file

  server_ip="$(read_env_value "$ENV_FILE" SERVER_IP 2>/dev/null || true)"

  install -d -m 0755 "$DATA_DIR/traefik"
  cidrs_file="$DATA_DIR/traefik/origin-shield.yml"

  local ip_host_rule=""
  if [[ -n "$server_ip" && "$server_ip" != "127.0.0.1" && "$server_ip" != "::1" ]]; then
    ip_host_rule=" || Host(\`${server_ip}\`)"
  fi
  local block_rule="HostRegexp(\`^([0-9]{1,3}\\\\.){3}[0-9]{1,3}\$\`) || HostRegexp(\`^\\\\[[0-9a-fA-F:.]+\\\\]\$\`)${ip_host_rule}"

  cat >"$cidrs_file" <<YAML
# pushify-origin-shield-v4 — block raw IP Host only (edge limits in security-middlewares.yml)
http:
  middlewares:
    pushify-shield-deny:
      headers:
        customResponseHeaders:
          Server: ""
          X-Powered-By: ""
  routers:
    pushify-shield-ip-http:
      rule: "${block_rule}"
      entryPoints:
        - web
      priority: 32766
      service: noop@internal
      middlewares:
        - pushify-shield-deny
    pushify-shield-ip-https:
      rule: "${block_rule}"
      entryPoints:
        - websecure
      priority: 32766
      service: noop@internal
      middlewares:
        - pushify-shield-deny
YAML

  chmod 0644 "$cidrs_file" 2>/dev/null || true
  if [[ "$ENVIRONMENT" == "production" ]]; then
    service_user="$(default_service_user)"
    chown "$service_user:$service_user" "$cidrs_file" 2>/dev/null || true
  fi
}

remove_traefik_origin_shield() {
  local f="$DATA_DIR/traefik/origin-shield.yml"
  [[ -f "$f" ]] && rm -f "$f"
}

apply_ufw_hardening() {
  command -v ufw >/dev/null 2>&1 || return 0

  local ssh_port allow_ssh cidr
  ssh_port="$(read_env_value "$ENV_FILE" DEVPUSH_SSH_PORT 2>/dev/null || true)"
  ssh_port="${ssh_port:-22}"
  allow_ssh="$(read_env_value "$ENV_FILE" DEVPUSH_UFW_ALLOW_SSH 2>/dev/null || true)"
  allow_ssh="${allow_ssh:-1}"
  cidr="$(read_env_value "$ENV_FILE" DEVPUSH_SSH_ALLOW_CIDR 2>/dev/null || true)"

  if ! ufw status 2>/dev/null | grep -qE '^Status: active'; then
    run_cmd "${CHILD_MARK} Configuring UFW defaults" ufw --force reset
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
  fi

  ufw_allow_once() {
    local rule="$1"
    ufw status numbered 2>/dev/null | grep -qF "$rule" && return 0
    run_cmd "${CHILD_MARK} UFW allow ${rule}" ufw allow "$rule"
  }

  ufw_allow_once "80/tcp"
  ufw_allow_once "443/tcp"

  if [[ "$allow_ssh" == "1" || "$allow_ssh" == "true" || "$allow_ssh" == "yes" ]]; then
    if [[ -n "$cidr" ]]; then
      local ssh_rule="from ${cidr} to any port ${ssh_port} proto tcp"
      ufw status numbered 2>/dev/null | grep -qF "$cidr" || \
        run_cmd "${CHILD_MARK} UFW allow SSH from ${cidr}" ufw allow from "$cidr" to any port "$ssh_port" proto tcp
    else
      ufw_allow_once "${ssh_port}/tcp"
    fi
  fi

  if ! ufw status 2>/dev/null | grep -qE '^Status: active'; then
    run_cmd "${CHILD_MARK} Enabling UFW" ufw --force enable
  fi
}

install_origin_shield() {
  [[ "${ENVIRONMENT:-production}" == "production" ]] || return 0

  if origin_shield_enabled; then
    run_cmd "${CHILD_MARK} Writing Traefik origin shield" write_traefik_origin_shield
    if [[ $EUID -eq 0 ]] && origin_shield_ufw_enabled; then
      apply_ufw_hardening
    elif origin_shield_ufw_enabled && [[ $EUID -ne 0 ]]; then
      printf "${YEL}%s UFW skipped (run as root to harden host firewall).${NC}\n" "${CHILD_MARK}"
    fi
  else
    remove_traefik_origin_shield
  fi
}

install_origin_shield
