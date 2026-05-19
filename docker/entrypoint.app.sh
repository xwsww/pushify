#!/bin/sh
set -e

workers="${UVICORN_WORKERS:-2}"
# Trust only Traefik/Docker networks (not clients). Traefik sets X-Forwarded-For / CF-Connecting-IP.
forwarded_allow="${UVICORN_FORWARDED_ALLOW_IPS:-172.16.0.0/12,10.0.0.0/8,127.0.0.1,::1/128}"
exec uv run uvicorn main:app --host 0.0.0.0 --port 8000 --loop uvloop --http httptools \
  --workers "$workers" --proxy-headers --forwarded-allow-ips="$forwarded_allow" --no-access-log