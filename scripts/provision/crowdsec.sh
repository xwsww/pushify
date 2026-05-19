#!/usr/bin/env bash
# Install CrowdSec + iptables/ipset firewall bouncer (host-level L3/L4).
set -Eeuo pipefail
IFS=$'\n\t'

[[ $EUID -eq 0 ]] || { printf "crowdsec.sh must run as root.\n" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$SCRIPT_DIR/../lib.sh"

DATA_DIR="${DATA_DIR:-/var/lib/devpush}"
TRAEFIK_LOG_DIR="$DATA_DIR/traefik/logs"
ACCESS_LOG="$TRAEFIK_LOG_DIR/access.log"
ENV_FILE="${ENV_FILE:-$DATA_DIR/.env}"

ensure_traefik_log_dir

panel_host=""
pma_host=""
if [[ -f "$ENV_FILE" ]]; then
  panel_host="$(read_env_value "$ENV_FILE" APP_HOSTNAME 2>/dev/null || true)"
  pma_host="$(read_env_value "$ENV_FILE" PHPMYADMIN_HOSTNAME 2>/dev/null || true)"
fi
[[ -n "$panel_host" ]] || panel_host="panel.local"
[[ -n "$pma_host" ]] || pma_host="db.${panel_host}"

apt_pkg_available() {
  apt-cache show "$1" &>/dev/null
}

ensure_crowdsec_apt_repo() {
  local list="/etc/apt/sources.list.d/crowdsec_crowdsec.list"
  if [[ -f "$list" ]] && grep -q 'crowdsec/crowdsec/any' "$list" 2>/dev/null; then
    return 0
  fi
  # Legacy packagecloud scripts (crowdsecurity/* or distro-specific) miss bouncer on Ubuntu 24+.
  rm -f /etc/apt/sources.list.d/crowdsecurity_crowdsec.list 2>/dev/null || true
  run_cmd "${CHILD_MARK} Adding CrowdSec apt repository (any/any)" \
    bash -c 'curl -fsSL https://install.crowdsec.net | sh'
}

ensure_crowdsec_apt_pin() {
  local pin="/etc/apt/preferences.d/crowdsec-pushify"
  [[ -f "$pin" ]] && return 0
  install -d -m 0755 /etc/apt/preferences.d
  cat >"$pin" <<'EOF'
# pushify-crowdsec-v3 — prefer official CrowdSec repo over distro packages
Package: crowdsec crowdsec-firewall-bouncer-iptables crowdsec-firewall-bouncer
Pin: release o=packagecloud.io/crowdsec/crowdsec,a=any,n=any,c=main
Pin-Priority: 1001
EOF
}

pick_firewall_bouncer_pkg() {
  local pkg
  for pkg in crowdsec-firewall-bouncer-iptables crowdsec-firewall-bouncer; do
    if apt_pkg_available "$pkg"; then
      printf '%s' "$pkg"
      return 0
    fi
  done
  return 1
}

install_crowdsec_packages() {
  ensure_crowdsec_apt_repo
  ensure_crowdsec_apt_pin
  run_cmd "${CHILD_MARK} Refreshing apt cache" apt-get update -yq

  local bouncer_pkg
  if ! bouncer_pkg="$(pick_firewall_bouncer_pkg)"; then
    err "No CrowdSec firewall bouncer package in apt (need crowdsec/crowdsec any/any repo)."
    return 1
  fi

  if command -v cscli >/dev/null 2>&1; then
    run_cmd "${CHILD_MARK} Ensuring CrowdSec packages" \
      apt-get install -yq crowdsec "$bouncer_pkg" ipset
  else
    run_cmd "${CHILD_MARK} Installing CrowdSec packages" \
      apt-get install -yq crowdsec "$bouncer_pkg" ipset
  fi
}

if ! install_crowdsec_packages; then
  exit 1
fi

PUSHIFY_BOUNCER_MARKER="pushify-crowdsec-v5"
BOUNCER_CFG="/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml"
BOUNCER_LAPI_NAME="crowdsec-firewall-bouncer"

bouncer_config_ok() {
  [[ -f "$BOUNCER_CFG" ]] || return 1
  grep -q "$PUSHIFY_BOUNCER_MARKER" "$BOUNCER_CFG" 2>/dev/null || return 1
  grep -qE '^[[:space:]]*api_url:[[:space:]]*http' "$BOUNCER_CFG" 2>/dev/null || return 1
  local key
  key="$(grep -E '^[[:space:]]*api_key:' "$BOUNCER_CFG" 2>/dev/null | head -1 | sed -E 's/^[[:space:]]*api_key:[[:space:]]*//' | tr -d "\"'")"
  [[ -n "$key" && "$key" != *'API_KEY'* && "$key" != *'${'* ]]
}

read_bouncer_api_key() {
  grep -E '^[[:space:]]*api_key:' "$BOUNCER_CFG" 2>/dev/null | head -1 | sed -E 's/^[[:space:]]*api_key:[[:space:]]*//' | tr -d "\"'"
}

delete_stale_firewall_bouncers() {
  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    run_cmd --try "${CHILD_MARK} Removing stale bouncer $name" cscli bouncers delete "$name"
  done < <(
    cscli bouncers list 2>/dev/null | awk 'NR>1 {print $1}' | grep -iE 'firewall|cs-firewall-bouncer' || true
  )
}

register_firewall_bouncer_key() {
  local api_key=""
  api_key="$(cscli bouncers add "$BOUNCER_LAPI_NAME" -o raw 2>/dev/null | tr -d '[:space:]')"
  if [[ -z "$api_key" ]]; then
    err "Could not register CrowdSec firewall bouncer (cscli bouncers add failed)."
    return 1
  fi
  printf '%s' "$api_key"
}

write_firewall_bouncer_config() {
  local api_key="$1"
  install -d -m 0750 /etc/crowdsec/bouncers
  cat >"$BOUNCER_CFG" <<YAML
# $PUSHIFY_BOUNCER_MARKER
mode: iptables
update_frequency: 10s
log_mode: file
log_dir: /var/log/
log_level: info
api_url: http://127.0.0.1:8080/
api_key: ${api_key}
deny_action: DROP
disable_ipv6: true
blacklists_ipv4: crowdsec-blacklists
ipset_type: nethash
iptables_chains:
  - INPUT
  - DOCKER-USER
iptables_add_rule_comments: true
supported_decisions_types:
  - ban
# Never ban private/Docker ranges (panel <-> containers stay reachable)
ipset_whitelist:
  127.0.0.0/8:
  10.0.0.0/8:
  172.16.0.0/12:
  192.168.0.0/16:
YAML
  chmod 600 "$BOUNCER_CFG"
}

install_firewall_bouncer_config() {
  local api_key=""
  if bouncer_config_ok; then
    return 0
  fi
  if [[ -f "$BOUNCER_CFG" ]] && api_key="$(read_bouncer_api_key)" && [[ -n "$api_key" ]]; then
    :
  else
    delete_stale_firewall_bouncers
    api_key="$(register_firewall_bouncer_key)" || return 1
  fi
  write_firewall_bouncer_config "$api_key"
  if ! command -v crowdsec-firewall-bouncer >/dev/null 2>&1; then
    err "crowdsec-firewall-bouncer binary not found."
    return 1
  fi
  if ! crowdsec-firewall-bouncer -c "$BOUNCER_CFG" -t >/dev/null 2>&1; then
    err "Firewall bouncer config test failed ($BOUNCER_CFG)."
    return 1
  fi
}

wait_for_crowdsec_lapi() {
  local i
  for i in $(seq 1 45); do
    if cscli version >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  err "CrowdSec LAPI not ready (cscli unavailable)."
  return 1
}

run_cmd "${CHILD_MARK} Installing CrowdSec collections" cscli collections install \
  crowdsecurity/traefik \
  crowdsecurity/linux \
  crowdsecurity/base-http-scenarios \
  crowdsecurity/http-dos \
  || true

install_pushify_scenarios() {
  local src_dir="$APP_DIR/scripts/provision/crowdsec"
  local dst_dir="/etc/crowdsec/scenarios"
  [[ -d "$src_dir" ]] || return 0
  install -d -m 0755 "$dst_dir"
  local f base
  for f in "$src_dir"/*.yaml; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    if [[ ! -f "$dst_dir/$base" ]] || ! grep -q 'pushify-crowdsec-scenario' "$dst_dir/$base" 2>/dev/null; then
      install -m 0644 "$f" "$dst_dir/$base"
    fi
  done
}
install_pushify_scenarios

install -d -m 0755 /etc/crowdsec/acquis.d
cat >/etc/crowdsec/acquis.d/pushify-traefik.yaml <<EOF
# pushify-crowdsec-v3
filenames:
  - $ACCESS_LOG
labels:
  type: traefik
EOF

install -d -m 0755 /etc/crowdsec/postoverflows/s01-whitelist
cat >/etc/crowdsec/postoverflows/s01-whitelist/pushify-panel.yaml <<EOF
# pushify-crowdsec-v5
name: pushify/panel-whitelist
description: Whitelist panel/phpMyAdmin only (deploy sites are NOT exempt)
whitelist:
  reason: pushify_internal
  expression:
    - evt.Parsed.request_host == '${panel_host}'
    - evt.Parsed.request_host == '${pma_host}' && evt.Parsed.request_uri startsWith '/'
    - evt.Parsed.request_host == '${panel_host}' && evt.Parsed.request_uri startsWith '/.pushify'
    - evt.Parsed.request_host == '${panel_host}' && evt.Parsed.request_uri startsWith '/security/'
    - evt.Parsed.request_host == '${panel_host}' && evt.Parsed.request_uri startsWith '/auth/'
    - evt.Parsed.request_host == '${panel_host}' && evt.Parsed.request_uri startsWith '/health'
EOF

logrotate_src="$APP_DIR/scripts/provision/logrotate-traefik"
if [[ -f "$logrotate_src" ]]; then
  install -m 0644 "$logrotate_src" /etc/logrotate.d/pushify-traefik
fi

run_cmd "${CHILD_MARK} Enabling CrowdSec agent" systemctl enable --now crowdsec
wait_for_crowdsec_lapi || exit 1

if systemctl list-unit-files crowdsec-firewall-bouncer.service &>/dev/null; then
  if install_firewall_bouncer_config; then
    run_cmd "${CHILD_MARK} Enabling firewall bouncer" systemctl enable --now crowdsec-firewall-bouncer
  else
    printf "${YEL}%s Firewall bouncer not configured — CrowdSec detections still work without iptables bans.${NC}\n" "${CHILD_MARK}"
  fi
fi

if command -v cscli >/dev/null 2>&1; then
  run_cmd --try "${CHILD_MARK} Reloading CrowdSec" systemctl reload crowdsec || \
    systemctl restart crowdsec
  if systemctl list-unit-files crowdsec-firewall-bouncer.service &>/dev/null; then
    run_cmd --try "${CHILD_MARK} Restarting firewall bouncer" systemctl restart crowdsec-firewall-bouncer
  fi
fi

printf "${GRN}CrowdSec + iptables/ipset bouncer ready (logs: %s).${NC}\n" "$ACCESS_LOG"
