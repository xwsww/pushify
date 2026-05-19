from __future__ import annotations

from starlette.requests import Request


def client_ip(request: Request, *, behind_cloudflare: bool = False) -> str:
  if behind_cloudflare:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
      return cf.strip()
  forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
  if forwarded:
    return forwarded.split(",")[0].strip()
  if request.client and request.client.host:
    return request.client.host
  return ""
