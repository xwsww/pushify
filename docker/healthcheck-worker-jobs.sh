#!/bin/sh
set -e

if pgrep -f "arq workers.jobs.WorkerSettings" >/dev/null 2>&1; then
  exit 0
fi

exec uv run arq --check workers.jobs.WorkerSettings
