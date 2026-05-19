#!/usr/bin/env bash
# Shared helpers: Cloudflare / reverse-proxy trust (sourced, not executed).
[[ -n "${_PUSHIFY_PROXY_TRUST_LOADED:-}" ]] && return 0
_PUSHIFY_PROXY_TRUST_LOADED=1

devpush_env_bool() {
  local key="${1:?}" default="${2:-0}"
  local v="${!key:-}"
  if [[ -z "$v" && -f "${ENV_FILE:-}" ]]; then
    v="$(read_env_value "$ENV_FILE" "$key" 2>/dev/null || true)"
  fi
  [[ -n "$v" ]] || v="$default"
  [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" ]]
}

devpush_behind_cloudflare() {
  devpush_env_bool DEVPUSH_BEHIND_CLOUDFLARE 0
}

devpush_https_enabled() {
  devpush_env_bool DEVPUSH_ENABLE_HTTPS 1
}

# Prints one CIDR per line (no comments).
devpush_trusted_proxy_cidrs() {
  local trusted manual line
  trusted="$(read_env_value "${ENV_FILE:-}" DEVPUSH_TRUSTED_PROXY_CIDRS 2>/dev/null || true)"
  manual="$trusted"

  if devpush_behind_cloudflare || [[ "${trusted,,}" == "cloudflare" ]]; then
    local cf_list="${APP_DIR:-}/scripts/provision/cloudflare-ips.txt"
    if [[ -f "$cf_list" ]]; then
      grep -vE '^\s*#' "$cf_list" | grep -vE '^\s*$' || true
    fi
    if [[ "${trusted,,}" == "cloudflare" ]]; then
      manual=""
    fi
  fi

  if [[ -n "$manual" ]]; then
    local -a parts=()
    local c ip
    IFS=',' read -ra parts <<<"$manual"
    for c in "${parts[@]}"; do
      ip="${c#"${c%%[![:space:]]*}"}"
      ip="${ip%"${ip##*[![:space:]]}"}"
      [[ -n "$ip" ]] || continue
      printf '%s\n' "$ip"
    done
  fi
}

devpush_has_trusted_proxies() {
  [[ -n "$(devpush_trusted_proxy_cidrs | head -1)" ]]
}

# YAML fragment for rateLimit / inFlightReq sourceCriterion (2-space indent under parent).
devpush_traefik_ip_criterion_yaml() {
  if devpush_behind_cloudflare; then
    printf '%s\n' '          requestHeaderName: CF-Connecting-IP'
  else
    printf '%s\n' '          ipStrategy:' '            depth: 1'
  fi
}

devpush_traefik_entrypoints_yaml() {
  local line
  printf '%s\n' 'entryPoints:' '  web:' '    http:' '      middlewares:' '        - pushify-edge-guard@file'
  if devpush_has_trusted_proxies; then
    printf '%s\n' '    forwardedHeaders:' '      trustedIPs:' '        - "127.0.0.1/32"' '        - "::1/128"'
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      printf '        - "%s"\n' "$line"
    done < <(devpush_trusted_proxy_cidrs | sort -u)
  fi
  printf '%s\n' '  websecure:' '    http:' '      middlewares:' '        - pushify-edge-guard@file'
  if devpush_has_trusted_proxies; then
    printf '%s\n' '    forwardedHeaders:' '      trustedIPs:' '        - "127.0.0.1/32"' '        - "::1/128"'
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      printf '        - "%s"\n' "$line"
    done < <(devpush_trusted_proxy_cidrs | sort -u)
  fi
}
