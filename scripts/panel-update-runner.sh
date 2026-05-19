#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${DEVPUSH_APP_DIR:-/opt/devpush}"
DATA_DIR="${DEVPUSH_DATA_DIR:-/var/lib/devpush}"
STATUS_FILE="${DEVPUSH_UPDATE_STATUS_FILE:-$DATA_DIR/update-status.json}"
LOG_DIR="$DATA_DIR/logs"
LOG_FILE="$LOG_DIR/panel-update.log"
ref=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      ref="${2:-}"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

mkdir -p "$LOG_DIR"

write_status() {
  local state="$1"
  local message="$2"
  local current_ref="${3:-}"
  jq -n \
    --arg state "$state" \
    --arg message "$message" \
    --arg current_ref "$current_ref" \
    --arg target_ref "$ref" \
    --arg updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      state: $state,
      message: $message,
      current_ref: ($current_ref // ""),
      target_ref: ($target_ref // ""),
      updated_at: $updated_at
    }' > "$STATUS_FILE"
}

ensure_service_user() {
  local uid gid group_name user_name
  uid="$(stat -c '%u' "$APP_DIR")"
  gid="$(stat -c '%g' "$APP_DIR")"

  if getent group "$gid" >/dev/null 2>&1; then
    group_name="$(getent group "$gid" | cut -d: -f1)"
  else
    group_name="devpush"
    if getent group "$group_name" >/dev/null 2>&1; then
      group_name="devpush-helper"
    fi
    addgroup --gid "$gid" "$group_name" >/dev/null
  fi

  if getent passwd "$uid" >/dev/null 2>&1; then
    user_name="$(getent passwd "$uid" | cut -d: -f1)"
  else
    user_name="devpush"
    if id -u "$user_name" >/dev/null 2>&1; then
      user_name="devpush-helper"
    fi
    adduser --uid "$uid" --gid "$gid" --system --home "$APP_DIR" "$user_name" >/dev/null
  fi

  export DEVPUSH_SERVICE_USER="$user_name"
}

current_ref=""
if [[ -f "$DATA_DIR/version.json" ]]; then
  current_ref="$(jq -r '.git_ref // empty' "$DATA_DIR/version.json" 2>/dev/null || true)"
fi

on_error() {
  local exit_code=$?
  write_status "failed" "Update failed. Check panel-update.log for details." "$current_ref"
  exit "$exit_code"
}
trap on_error ERR

write_status "starting" "Preparing update environment..." "$current_ref"
ensure_service_user
write_status "running" "Applying update. The panel will reconnect automatically." "$current_ref"

bash "$APP_DIR/scripts/update.sh" --ref "$ref" --yes --no-telemetry >>"$LOG_FILE" 2>&1

current_ref="$(jq -r '.git_ref // empty' "$DATA_DIR/version.json" 2>/dev/null || true)"
write_status "completed" "Update finished successfully. Reloading the panel..." "$current_ref"
